import os

import pytest

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


def test_synthesis_uses_system_package_phys_top_and_fails_before_sta_on_missing_netlist(tmp_path, monkeypatch):
    rtl = tmp_path / "temp_monitor_soc_phys.sv"
    sdc = tmp_path / "top.sdc"
    rtl.write_text("module temp_monitor_soc_phys(input clk); endmodule\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")

    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (2, "ERROR: Module `top' not found!\n"))

    with pytest.raises(RuntimeError, match="synthesis failed before downstream PD stages"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "System_Synthesis",
            "digital": {
                "top_module": "top",
                "rtl_files": [str(rtl)],
                "constraints_sdc": str(sdc),
            },
            "system_rtl_package": {
                "top": {"phys": "temp_monitor_soc_phys", "sim": "temp_monitor_soc_sim"},
            },
        })

    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    config = (tmp_path / "digital" / "synth" / "config.json").read_text(encoding="utf-8")
    assert '"top_module": "temp_monitor_soc_phys"' in summary
    assert '"DESIGN_NAME": "temp_monitor_soc_phys"' in config


def test_arch2synthesis_uses_digital_top_even_when_system_package_exists(tmp_path, monkeypatch):
    rtl = tmp_path / "digital_top.sv"
    sdc = tmp_path / "digital_top.sdc"
    rtl.write_text("module digital_top(input clk); endmodule\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")

    monkeypatch.setattr(agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")
    monkeypatch.setattr(agent, "_run", lambda *args, **kwargs: (2, "synthesis failed after selecting top\n"))

    with pytest.raises(RuntimeError, match="synthesis failed before downstream PD stages"):
        agent.run_agent({
            "workflow_id": "wf",
            "workflow_dir": str(tmp_path),
            "workflow_name": "Digital_Synthesis",
            "digital": {
                "top_module": "digital_top",
                "rtl_files": [str(rtl)],
                "constraints_sdc": str(sdc),
            },
            "system_rtl_package": {
                "top": {"phys": "temp_monitor_soc_phys", "sim": "temp_monitor_soc_sim"},
            },
        })

    summary = (tmp_path / "digital" / "synth" / "synth_summary.json").read_text(encoding="utf-8")
    config = (tmp_path / "digital" / "synth" / "config.json").read_text(encoding="utf-8")
    assert '"top_module": "digital_top"' in summary
    assert '"DESIGN_NAME": "digital_top"' in config
