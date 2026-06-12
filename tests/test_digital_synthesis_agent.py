import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_synthesis_agent as agent


def test_repair_common_status_tieoffs_adds_safe_assignments(tmp_path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "wire status_packet_active;\n"
        "wire status_error;\n"
        "wire status_tx_busy;\n"
        "wire status_rx_busy;\n"
        "wire rx_overflow_event;\n"
        "wire tx_underflow_event;\n"
        "endmodule\n",
        encoding="utf-8",
    )

    repairs = agent._repair_common_status_tieoffs(str(rtl))
    text = rtl.read_text(encoding="utf-8")

    assert repairs == [
        "assign status_packet_active = status_tx_busy | status_rx_busy;",
        "assign status_error = rx_overflow_event | tx_underflow_event;",
    ]
    assert "assign status_packet_active = status_tx_busy | status_rx_busy;" in text
    assert "assign status_error = rx_overflow_event | tx_underflow_event;" in text
