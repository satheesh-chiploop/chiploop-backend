import sys
import types
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The sanitizer is pure; avoid requiring Supabase credentials merely to import
# its module in a unit test.
artifact_stub = types.ModuleType("utils.artifact_utils")
artifact_stub.save_text_artifact_and_record = lambda *args, **kwargs: None
sys.modules.setdefault("utils.artifact_utils", artifact_stub)

from agents.digital.digital_rtl_agent import (
    _flatten_constant_part_select_bit_selects,
    _remove_comb_blocking_assigns_to_sequential_regs,
    _repair_empty_case_statements,
    _sanitize_single_driver_rtl,
)


def test_relational_less_equal_is_not_treated_as_nonblocking_assignment():
    rtl = """
reg [31:0] age_calc;
reg timeout;

always @(*) begin
    age_calc = 32'd0;
    if (rsp_timestamp >= req_timestamp)
        age_calc = rsp_timestamp[31:0] - req_timestamp[31:0];
end

always @(posedge clk) begin
    timeout <= 1'b0;
    if (age_calc <= timeout_threshold_cycles)
        timeout <= 1'b1;
end
"""

    sanitized = _remove_comb_blocking_assigns_to_sequential_regs(rtl)

    assert "age_calc = 32'd0;" in sanitized
    assert "age_calc = rsp_timestamp[31:0] - req_timestamp[31:0];" in sanitized
    assert "if (age_calc <= timeout_threshold_cycles)" in sanitized


def test_real_comb_assignment_to_sequential_target_is_removed():
    rtl = """
reg state;
always @(*) begin
    state = next_state;
end
always @(posedge clk) begin
    state <= next_state;
end
"""

    sanitized = _remove_comb_blocking_assigns_to_sequential_regs(rtl)

    assert "state = next_state;" not in sanitized
    assert "state <= next_state;" in sanitized


def test_reset_only_write_does_not_destroy_combinational_readback_case():
    rtl = """
module readback(input clk, input rst_n, input [7:0] addr, output [31:0] rdata);
reg [31:0] rdata_reg;
assign rdata = rdata_reg;
always @(*) begin
    rdata_reg = 32'h0;
    case (addr)
        8'h00: rdata_reg = 32'h12345678;
        default: rdata_reg = 32'h0;
    endcase
end
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        rdata_reg <= 32'h0;
    end
end
endmodule
"""

    sanitized = _sanitize_single_driver_rtl({"readback.v": rtl})["readback.v"]

    assert "8'h00: rdata_reg = 32'h12345678;" in sanitized
    assert "default: rdata_reg = 32'h0;" in sanitized
    assert "rdata_reg <= 32'h0;" not in sanitized


def test_constant_chained_part_select_is_flattened():
    rtl = "if (rsp_payload_reg[127:96][0]) valid = 1'b1;"

    assert _flatten_constant_part_select_bit_selects(rtl) == (
        "if (rsp_payload_reg[96]) valid = 1'b1;"
    )


def test_empty_case_left_by_driver_cleanup_gets_legal_default_item():
    rtl = """
always @(*) begin
    case (cfg_addr)
    endcase
end
"""

    repaired = _repair_empty_case_statements(rtl)

    assert "case (cfg_addr)\n        default: ;\n    endcase" in repaired
