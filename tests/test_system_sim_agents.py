import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_sim_execution_agent as execution_agent
from agents.system import system_formal_verification_agent as formal_agent
from agents.system import system_testbench_generator_agent as tb_agent


def test_system_tb_makefile_matches_digital_verify_contract():
    text = tb_agent._gen_makefile("temp_monitor_soc_sim")

    assert "override TOPLEVEL      := temp_monitor_soc_sim" in text
    assert "override HDL_TOPLEVEL  := temp_monitor_soc_sim" in text
    assert "override VERILATOR_TOPLEVEL := temp_monitor_soc_sim" in text
    assert "EXTRA_ARGS += --trace --trace-structs" in text
    assert "EXTRA_ARGS += --coverage" in text
    assert "EXTRA_ARGS += --assert" in text
    assert "--vpi" not in text
    assert "--prefix Vtop" not in text
    assert "cocotb_vpi_bootstrap" not in text


def test_system_sim_execution_matches_digital_verify_make_invocation(tmp_path, monkeypatch):
    tb_root = tmp_path / "vv" / "tb"
    tb_root.mkdir(parents=True)
    (tb_root / "Makefile").write_text("all:\n\t@echo ok\n", encoding="utf-8")
    (tb_root / "simulation_manifest.json").write_text(
        '{"top_module":"temp_monitor_soc_sim","default_tests":["system_smoke_test"]}',
        encoding="utf-8",
    )
    captured = {}

    monkeypatch.setattr(execution_agent, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(execution_agent, "_has_cocotb_runtime", lambda: True)
    monkeypatch.setattr(execution_agent, "_python_has_module", lambda name: True)
    monkeypatch.setattr(execution_agent, "_collect_code_coverage", lambda *args, **kwargs: {"status": "missing"})
    monkeypatch.setattr(execution_agent, "_record", lambda *args, **kwargs: None)

    def fake_run(cmd, cwd=None, env=None, timeout=1800):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return {
            "cmd": cmd,
            "cwd": cwd,
            "returncode": 0,
            "stdout": "0 failed\n",
            "stderr": "",
            "runtime_sec": 0.1,
        }

    monkeypatch.setattr(execution_agent, "_run", fake_run)

    state = {
        "workflow_id": "system-sim",
        "workflow_dir": str(tmp_path),
        "system_sim_seeds": [1],
    }

    execution_agent.run_agent(state)

    assert captured["cmd"] == ["make", "TESTCASE=system_smoke_test"]
    assert captured["env"]["TOPLEVEL"] == "temp_monitor_soc_sim"
    assert captured["env"]["HDL_TOPLEVEL"] == "temp_monitor_soc_sim"


def test_system_tb_parser_preserves_logic_vector_widths(tmp_path):
    rtl = tmp_path / "soc_top_sim.sv"
    rtl.write_text(
        """module temp_monitor_soc_sim (
  input logic clk,
  input logic [7:0] wr_addr,
  input logic [15:0] wr_data,
  output logic [11:0] temp_code
);
endmodule
""",
        encoding="utf-8",
    )

    ports = {p["name"]: p for p in tb_agent._ports_from_top_sv(str(rtl), "temp_monitor_soc_sim")}

    assert ports["wr_addr"]["width"] == "((7) - (0) + 1)"
    assert ports["wr_data"]["width"] == "((15) - (0) + 1)"
    assert ports["temp_code"]["width"] == "((11) - (0) + 1)"


def test_system_formal_blackboxes_adc_models_under_workflow_digital_path():
    path = "/tmp/artifacts/digital/system/imported_rtl/temp_sensor_adc_model.v"

    assert formal_agent._is_analog_or_macro_file(path) is True
