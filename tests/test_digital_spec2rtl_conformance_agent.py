import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_spec2rtl_conformance_agent as agent


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
