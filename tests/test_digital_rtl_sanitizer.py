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

from agents.digital.digital_rtl_agent import _remove_comb_blocking_assigns_to_sequential_regs


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
