import os

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_spec2rtl_conformance_agent as agent


def test_match_score_recognizes_programmable_period_rollover_logic():
    rtl = """
reg [7:0] counter_reg;
always @(posedge clk) begin
  if (!reset_n) counter_reg <= 8'h00;
  else if (enable) begin
    if (counter_reg == period) counter_reg <= 8'h00;
    else counter_reg <= counter_reg + 8'h01;
  end
end
"""
    names = set(rtl.replace(";", " ").replace("(", " ").replace(")", " ").split())

    status, evidence = agent._match_score(
        "Support programmable period-based rollover behavior.", rtl, names
    )

    assert status == "matched"
    assert "period_rollover_logic" in evidence


def test_match_score_recognizes_enabled_periodic_count_sequence():
    rtl = """
always @(posedge clk) begin
  if (!reset_n) begin
    state_q <= 8'h00;
  end else if (run_enable) begin
    if (state_q == terminal_limit) state_q <= 8'h00;
    else state_q <= state_q + 8'h01;
  end
end
"""

    status, evidence = agent._match_score(
        "Generate a periodic count sequence under enable control.",
        rtl,
        {"clk", "reset_n", "run_enable", "state_q", "terminal_limit"},
    )

    assert status == "matched"
    assert "enabled_periodic_count_sequence" in evidence


def test_match_score_proves_absent_hierarchy_and_memory_macros():
    rtl = "module pwm_controller(input clk, output pwm_out); assign pwm_out = clk; endmodule"

    status, evidence = agent._match_score(
        "The module has no internal hierarchy and no memory macros.", rtl, {"clk", "pwm_out"}
    )

    assert status == "matched"
    assert "no_internal_hierarchy" in evidence
    assert "no_memories" in evidence

    hierarchical_rtl = rtl.replace(
        "assign pwm_out = clk;", "child u_child(.clk(clk), .out(pwm_out));"
    )
    failed_status, _ = agent._match_score(
        "The module has no internal hierarchy and no memory macros.",
        hierarchical_rtl,
        {"clk", "pwm_out"},
    )
    assert failed_status == "missing"


def test_match_score_proves_coordinated_negative_structure_list():
    rtl = """
module pwm_controller(input clk, input [7:0] period, output pwm_out);
  assign pwm_out = clk & (period != 8'h00);
endmodule
"""

    status, evidence = agent._match_score(
        "The design contains no memories, buses, or submodules.",
        rtl,
        {"clk", "period", "pwm_out"},
    )

    assert status == "matched"
    assert "no_memories" in evidence
    assert "no_bus_interfaces" in evidence
    assert "no_internal_hierarchy" in evidence


def test_match_score_rejects_negative_structure_list_when_memory_exists():
    rtl = """
module queue(input clk, input [2:0] addr, output [7:0] data);
  reg [7:0] storage [0:7];
  assign data = storage[addr];
endmodule
"""

    status, evidence = agent._match_score(
        "The design contains no memories, buses, or submodules.",
        rtl,
        {"clk", "addr", "data", "storage"},
    )

    assert status == "missing"
    assert "no_memories" not in evidence


def test_match_score_proves_no_hidden_state_memory_or_extra_interface():
    rtl = """
module controller(input clk, output [7:0] count_out);
  reg [7:0] count_q;
  always @(posedge clk) begin
    count_q <= count_q + 8'h01;
  end
  assign count_out = count_q;
endmodule
"""
    context = {"output_ports": {"count_out"}, "interface_exact": True}

    status, evidence = agent._match_score(
        "Avoid any hidden state, memory, or extra handshakes beyond the declared interface.",
        rtl,
        {"clk", "count_out", "count_q"},
        context,
    )

    assert status == "matched"
    assert "no_hidden_sequential_state" in evidence
    assert "no_memories" in evidence
    assert "no_extra_interface_handshakes" in evidence


def test_match_score_rejects_unobservable_sequential_state():
    rtl = """
module controller(input clk, output done);
  reg hidden_q;
  always @(posedge clk) hidden_q <= ~hidden_q;
  assign done = 1'b0;
endmodule
"""

    status, evidence = agent._match_score(
        "Avoid hidden state or extra handshakes beyond the declared interface.",
        rtl,
        {"clk", "done", "hidden_q"},
        {"output_ports": {"done"}, "interface_exact": True},
    )

    assert status == "missing"
    assert "no_hidden_sequential_state" not in evidence


def test_hidden_state_checker_sees_later_assignment_after_nested_blocks():
    rtl = """
module controller(input clk, input reset_n, output visible);
  reg visible_q;
  reg hidden_q;
  always @(posedge clk) begin
    if (!reset_n) begin
      visible_q <= 1'b0;
    end else begin
      visible_q <= 1'b1;
    end
    hidden_q <= ~hidden_q;
  end
  assign visible = visible_q;
endmodule
"""

    status, evidence = agent._match_score(
        "Avoid hidden state beyond the declared interface.",
        rtl,
        {"clk", "reset_n", "visible", "visible_q", "hidden_q"},
        {"output_ports": {"visible"}, "interface_exact": True},
    )

    assert status == "missing"
    assert "no_hidden_sequential_state" not in evidence


def test_hidden_state_only_requirement_does_not_imply_no_memory_requirement():
    rtl = """
module storage(input clk, output [7:0] data);
  reg [7:0] memory [0:3];
  reg [7:0] data_q;
  always @(posedge clk) data_q <= memory[0];
  assign data = data_q;
endmodule
"""

    status, evidence = agent._match_score(
        "Avoid hidden state beyond the declared interface.",
        rtl,
        {"clk", "data", "data_q", "memory"},
        {"output_ports": {"data"}, "interface_exact": True},
    )

    assert status == "matched"
    assert "no_memories" not in evidence


def test_match_score_recognizes_negative_structure_synonyms_and_ignores_comments():
    rtl = """
// This module intentionally has no child implementation.
module controller(input clk, output done);
  assign done = clk;
endmodule
"""
    variants = [
        "The controller contains neither RAM nor child modules.",
        "The controller is free of storage arrays and component instances.",
        "Implement the block without memory or hierarchy.",
    ]

    for requirement in variants:
        status, _ = agent._match_score(requirement, rtl, {"clk", "done"})
        assert status == "matched", requirement


def test_match_score_detects_prefixed_protocol_bus_signals():
    rtl = """
module controller(input clk, input s_axi_awvalid, input [31:0] s_axi_awaddr, output s_axi_awready);
  assign s_axi_awready = s_axi_awvalid & clk;
endmodule
"""

    status, evidence = agent._match_score(
        "The controller has no memory, bus, or submodule instances.",
        rtl,
        {"clk", "s_axi_awvalid", "s_axi_awaddr", "s_axi_awready"},
    )

    assert status == "missing"
    assert "no_bus_interfaces" not in evidence


def test_match_score_proves_single_clock_synchronous_design_without_clock_gating():
    rtl = """
module controller(input clk, input reset_n, input enable, output reg done);
  always @(posedge clk) begin
    if (!reset_n) done <= 1'b0;
    else if (enable) done <= 1'b1;
  end
endmodule
"""
    requirement = "The design is fully synchronous to clk and contains no internal clock gating."

    status, evidence = agent._match_score(requirement, rtl, {"clk", "reset_n", "enable", "done"})

    assert status == "matched"
    assert "fully_synchronous_to_clk" in evidence
    assert "no_internal_clock_gating" in evidence


def test_match_score_rejects_derived_gated_clock():
    rtl = """
module controller(input clk, input enable, output reg done);
  wire gated_clk = clk & enable;
  always @(posedge gated_clk) done <= 1'b1;
endmodule
"""
    requirement = "The design is fully synchronous to clk and contains no internal clock gating."

    status, evidence = agent._match_score(requirement, rtl, {"clk", "enable", "done", "gated_clk"})

    assert status == "missing"
    assert "fully_synchronous_to_clk" not in evidence
    assert "no_internal_clock_gating" not in evidence


def test_match_score_rejects_async_reset_for_synchronous_requirement():
    rtl = """
always @(posedge clk or negedge reset_n) begin
  if (!reset_n) counter_value <= 8'h00;
end
"""

    status, evidence = agent._match_score(
        "Synchronously clear the counter to zero when reset_n is low.",
        rtl,
        {"clk", "reset_n", "counter_value"},
    )

    assert status == "missing"
    assert evidence == ["asynchronous_reset_sensitivity_conflicts_with_synchronous_requirement"]


def test_match_score_requires_named_combinational_output_to_be_reset_gated():
    requirement = (
        "When reset_n is low, the counter state is synchronously cleared to zero "
        "and pwm_out is driven low."
    )
    ungated = """
module pwm_controller(input reset_n, input [7:0] duty_cycle, output pwm_out);
  reg [7:0] counter_value_r;
  always @(posedge clk) if (!reset_n) counter_value_r <= 8'h00;
  assign pwm_out = counter_value_r < duty_cycle;
endmodule
"""
    gated = ungated.replace(
        "assign pwm_out = counter_value_r < duty_cycle;",
        "assign pwm_out = reset_n && (counter_value_r < duty_cycle);",
    )

    bad_status, bad_evidence = agent._match_score(
        requirement, ungated, {"reset_n", "duty_cycle", "pwm_out", "counter_value_r"}
    )
    good_status, good_evidence = agent._match_score(
        requirement, gated, {"reset_n", "duty_cycle", "pwm_out", "counter_value_r"}
    )

    assert bad_status == "missing"
    assert bad_evidence == ["reset_low_not_implemented:pwm_out"]
    assert good_status == "matched"
    assert "reset_low_pwm_out" in good_evidence


def test_match_score_accepts_synchronous_reset_through_next_state_mux():
    rtl = """
module controller(input clk, input reset_n, input enable);
  reg [7:0] counter_r;
  wire [7:0] counter_next;
  assign counter_next = (!reset_n) ? 8'h00 : (enable ? counter_r + 1'b1 : counter_r);
  always @(posedge clk) begin
    counter_r <= counter_next;
  end
endmodule
"""

    status, evidence = agent._match_score(
        "Honor synchronous active-low reset by clearing all registers to zero.",
        rtl,
        {"clk", "reset_n", "enable", "counter_r", "counter_next"},
    )

    assert status == "matched"
    assert "all_sequential_state_synchronously_reset_zero" in evidence


def test_match_score_rejects_when_only_some_sequential_state_is_reset():
    rtl = """
module controller(input clk, input reset_n);
  reg state_a;
  reg state_b;
  always @(posedge clk) begin
    if (!reset_n) begin
      state_a <= 1'b0;
    end else begin
      state_a <= 1'b1;
    end
    state_b <= ~state_b;
  end
endmodule
"""

    status, evidence = agent._match_score(
        "Synchronously reset all registers to zero.",
        rtl,
        {"clk", "reset_n", "state_a", "state_b"},
    )

    assert status == "missing"
    assert evidence == ["not_all_sequential_state_has_synchronous_zero_reset"]


def test_reset_proof_does_not_take_zero_assignment_from_else_branch():
    rtl = """
module controller(input clk, input reset_n);
  reg state_a;
  reg state_b;
  always @(posedge clk) begin
    if (!reset_n) begin
      state_a <= 1'b0;
      state_b <= 1'b1;
    end else begin
      state_a <= 1'b1;
      state_b <= 1'b0;
    end
  end
endmodule
"""

    status, evidence = agent._match_score(
        "Synchronously reset all registers to zero.",
        rtl,
        {"clk", "reset_n", "state_a", "state_b"},
    )

    assert status == "missing"
    assert evidence == ["not_all_sequential_state_has_synchronous_zero_reset"]


def test_match_score_rejects_always_ff_async_reset_for_synchronous_requirement():
    rtl = """
always_ff @(posedge clk or negedge reset_n) begin
  if (!reset_n) state_q <= 1'b0;
  else state_q <= state_d;
end
"""

    status, evidence = agent._match_score(
        "Synchronously reset state_q when reset_n is low.",
        rtl,
        {"clk", "reset_n", "state_q", "state_d"},
    )

    assert status == "missing"
    assert evidence == ["asynchronous_reset_sensitivity_conflicts_with_synchronous_requirement"]


def test_negative_memory_requirement_detects_integer_array():
    rtl = """
module controller(input clk, output done);
  integer storage [0:15];
  assign done = 1'b0;
endmodule
"""

    status, evidence = agent._match_score(
        "The controller contains no memories.", rtl, {"clk", "done", "storage"}
    )

    assert status == "missing"
    assert "no_memories" not in evidence


def test_match_score_uses_structural_evidence_for_implementation_properties():
    rtl = """
module pwm_controller(input clk, input [7:0] period, output [7:0] counter_value);
reg [7:0] counter_reg;
assign counter_value = counter_reg;
always @(posedge clk) begin
  if (counter_reg >= period) counter_reg <= 8'h00;
  else counter_reg <= counter_reg + 8'h01;
end
endmodule
"""
    requirements = [
        "The design must remain synthesizable using only registers, comparators, and simple control logic.",
        "The design must not infer latches.",
        "All arithmetic is unsigned and 8-bit wide.",
        "The controller does not contain memory macros or hierarchical submodules.",
    ]

    for requirement in requirements:
        status, _ = agent._match_score(requirement, rtl, {"clk", "period", "counter_value", "counter_reg"})
        assert status == "matched", requirement


def test_match_score_requires_direct_combinational_pwm_compare():
    registered = """
reg pwm_out_r;
assign pwm_out = pwm_out_r;
always @(posedge clk) pwm_out_r <= (counter_reg < duty_cycle);
"""
    combinational = "assign pwm_out = reset_n && (counter_reg < duty_cycle);"
    requirement = "The comparison for pwm_out is level-based: pwm_out is 1 when counter_value < duty_cycle, else 0."

    bad_status, bad_evidence = agent._match_score(requirement, registered, {"pwm_out", "counter_value", "duty_cycle"})
    good_status, _ = agent._match_score(requirement, combinational, {"pwm_out", "counter_value", "duty_cycle"})

    assert bad_status == "missing"
    assert bad_evidence == ["pwm_out_combinational_compare_not_implemented"]
    assert good_status == "matched"


def test_match_score_accepts_one_hop_combinational_output_alias():
    rtl = """
reg pwm_decode;
assign pwm_out = pwm_decode;
always @(*) begin
  pwm_decode = (counter_reg < duty_cycle) ? 1'b1 : 1'b0;
end
"""
    requirement = "pwm_out is a combinational decode of the registered count and duty_cycle."

    status, _ = agent._match_score(
        requirement, rtl, {"pwm_out", "pwm_decode", "counter_reg", "duty_cycle"}
    )

    assert status == "matched"


def test_match_score_recognizes_high_level_temp_monitor_evidence():
    rtl = """
module temp_monitor_digital(
  output [11:0] temp_code,
  output [11:0] threshold_code
);
  reg status_sample_done_r;
  reg status_alert_pending_r;
  reg irq_status_sample_done_r;
  reg irq_status_alert_r;
  reg control_irq_enable_r;
  always @(posedge clk) begin
    control_irq_enable_r <= wr_data[2];
    status_sample_done_r <= 1'b1;
    status_alert_pending_r <= 1'b1;
    irq_status_sample_done_r <= 1'b1;
    irq_status_alert_r <= 1'b1;
  end
endmodule
"""
    names = set(rtl.replace(";", " ").replace("(", " ").replace(")", " ").split())

    status, evidence = agent._match_score(
        "Latch sticky status and interrupt indicators according to the specification.",
        rtl,
        names,
    )
    assert status == "matched"
    assert "sticky status/interrupt indicators" in evidence

    status, evidence = agent._match_score(
        "Expose latest filtered temperature and threshold on dedicated outputs.",
        rtl,
        names,
    )
    assert status == "matched"
    assert "dedicated temp_code/threshold_code outputs" in evidence

    status, evidence = agent._match_score(
        "CONTROL bit 2 IRQ_ENABLE is stored.",
        rtl,
        names,
    )
    assert status == "matched"
    assert "CONTROL.IRQ_ENABLE stored" in evidence


def test_match_score_recognizes_irq_clear_bit1_sample_done_signal():
    rtl = """
module irq_ctrl(
  input [1:0] irq_clear_pulse
);
  reg irq_status_sample_done;
  reg status_sample_done;
  always @(posedge clk) begin
    if (irq_clear_pulse[1]) begin
      irq_status_sample_done <= 1'b0;
      status_sample_done <= 1'b0;
    end
  end
endmodule
"""
    names = set(rtl.replace(";", " ").replace("(", " ").replace(")", " ").split())

    status, evidence = agent._match_score(
        "IRQ_CLEAR bit 1 clears IRQ_STATUS.sample_done and STATUS.sample_done.",
        rtl,
        names,
    )

    assert status == "matched"
    assert "IRQ_CLEAR.sample_done clear" in evidence


@pytest.mark.parametrize("requirement,expected_evidence", [
    ("Clamp actuator commands to programmable min/max limits.", "programmable_min_max_clamp"),
    ("Optionally apply simple slew-rate limiting when enabled.", "bounded_slew_delta"),
    ("Deassert actuator command validity on invalid, stale, timeout, reset, or fault conditions.", "output_validity_inhibition"),
    ("No fallback command or substitute command is allowed.", "no_fallback_value_and_validity_inhibited"),
])
def test_generic_application_control_behavior_proofs(requirement, expected_evidence):
    rtl = """
module control(input clk, input [31:0] pkt_command, input [31:0] min_bound,
 input [31:0] max_bound, input [7:0] slew_step, output reg actuator_cmd_valid);
reg [31:0] clamped_cmd, slew_cmd, last_cmd_reg, diff_val;
always @(*) begin
  clamped_cmd = pkt_command;
  if (pkt_command < min_bound) clamped_cmd = min_bound;
  else if (pkt_command > max_bound) clamped_cmd = max_bound;
  diff_val = clamped_cmd - last_cmd_reg;
  slew_cmd = clamped_cmd;
  if (diff_val > slew_step) slew_cmd = last_cmd_reg + slew_step;
end
always @(posedge clk) begin
  if (pkt_command[31]) actuator_cmd_valid <= 1'b1;
  else actuator_cmd_valid <= 1'b0;
end
endmodule
"""
    status, evidence = agent._match_score(requirement, rtl, set(), {})
    assert status == "matched"
    assert expected_evidence in evidence


def test_reset_prose_generic_state_word_is_not_treated_as_output():
    rtl = """
module top(input clk, input rst_n, output reg ready);
reg state;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin state <= 1'b0; ready <= 1'b0; end
  else ready <= state;
end
endmodule
"""
    status, evidence = agent._match_score(
        "On rst_n deassertion low, pending state is cleared and ready is deasserted.",
        rtl,
        {"clk", "rst_n", "ready", "state"},
        {"output_ports": {"ready"}},
    )
    assert status == "matched"
    assert "reset_low_not_implemented:state" not in evidence


def test_register_evidence_accepts_descriptive_field_suffix_alias(tmp_path):
    spec = {"register_contract": {"registers": [{
        "name": "TELEMETRY", "address": "0x18",
        "fields": [{"name": "telemetry_word", "lsb": 0, "msb": 31}],
    }]}}
    rtl = "always @(*) case (csr_addr) 8'h18: csr_rdata = telemetry_shadow; endcase"
    result = agent._register_evidence("", rtl, {}, spec, None)
    assert result["missing"] == []


def test_register_evidence_merges_duplicate_register_and_prefers_address():
    spec = {
        "register_contract": {
            "registers": [{"name": "CONTROL", "fields": [{"name": "enable"}]}],
        }
    }
    regmap = {
        "registers": [{"name": "CONTROL", "offset": "0x00", "fields": [{"name": "enable"}]}],
    }
    rtl = """
reg enable_r;
always @(*) begin
  case (csr_addr)
    8'h00: csr_rdata = {63'd0, enable_r};
    default: csr_rdata = 64'd0;
  endcase
end
"""

    evidence = agent._register_evidence("", rtl, {}, spec, regmap)

    assert evidence["expected_registers"] == ["CONTROL"]
    assert evidence["matched_registers"] == ["CONTROL"]
    assert evidence["expected_addresses"] == ["0x00"]
    assert evidence["matched_addresses"] == ["0x00"]


def test_feature_contracts_are_statically_bound_to_rtl_interface():
    spec = {
        "name": "counter_top",
        "ports": [
            {"name": "enable", "direction": "input", "width": 1},
            {"name": "count", "direction": "output", "width": 8},
        ],
        "feature_contracts": [{
            "id": "increment", "stimulus": {"enable": 1},
            "expected": {"count": {"min": 1}}, "within_cycles": 1,
        }],
    }
    modules = [{
        "name": "counter_top",
        "ports": [
            {"name": "enable", "direction": "input"},
            {"name": "count", "direction": "output"},
        ],
    }]

    result = agent._feature_contract_evidence(spec, modules, "counter_top")

    assert result["status"] == "pass"
    assert result["checked"] == 1
    assert result["features"][0]["stimulus_signals"] == ["enable"]
    assert result["features"][0]["expected_signals"] == ["count"]


def test_feature_contract_binding_rejects_missing_or_wrong_direction_ports():
    spec = {
        "name": "counter_top",
        "ports": [
            {"name": "enable", "direction": "input", "width": 1},
            {"name": "count", "direction": "output", "width": 8},
        ],
        "feature_contracts": [{
            "id": "increment", "stimulus": {"enable": 1},
            "expected": {"count": 1}, "within_cycles": 1,
        }],
    }
    modules = [{
        "name": "counter_top",
        "ports": [
            {"name": "enable", "direction": "output"},
        ],
    }]

    result = agent._feature_contract_evidence(spec, modules, "counter_top")

    assert result["status"] == "issues"
    feature = result["features"][0]
    assert feature["missing_expected_signals"] == ["count"]
    assert feature["wrong_stimulus_directions"] == ["enable"]
