import os

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


def test_match_score_proves_absent_hierarchy_and_memory_macros():
    rtl = "module pwm_controller(input clk, output pwm_out); assign pwm_out = clk; endmodule"

    status, evidence = agent._match_score(
        "The module has no internal hierarchy and no memory macros.", rtl, {"clk", "pwm_out"}
    )

    assert status == "matched"
    assert "no_internal_hierarchy" in evidence
    assert "no_memory_macros" in evidence

    hierarchical_rtl = rtl.replace(
        "assign pwm_out = clk;", "child u_child(.clk(clk), .out(pwm_out));"
    )
    failed_status, _ = agent._match_score(
        "The module has no internal hierarchy and no memory macros.",
        hierarchical_rtl,
        {"clk", "pwm_out"},
    )
    assert failed_status == "missing"


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
