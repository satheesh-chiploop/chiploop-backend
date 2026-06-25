import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_top_assembly_agent as agent


def test_build_system_filelists_drop_duplicate_pass_artifacts(tmp_path):
    analog_dir = tmp_path / "analog"
    rtl_dir = tmp_path / "rtl"
    system_dir = tmp_path / "system" / "integration"
    analog_dir.mkdir()
    rtl_dir.mkdir()
    system_dir.mkdir(parents=True)

    (analog_dir / "temp_sensor_adc_model.v").write_text(
        "module temp_sensor_adc_model(input sample_req, output adc_valid); endmodule\n",
        encoding="utf-8",
    )
    (analog_dir / "temp_sensor_adc_model_pass1.v").write_text(
        "module temp_sensor_adc_model(input sample_req, output adc_valid); endmodule\n",
        encoding="utf-8",
    )
    (rtl_dir / "temp_monitor_digital.v").write_text(
        "module temp_monitor_digital(input clk); endmodule\n",
        encoding="utf-8",
    )
    (system_dir / "temp_monitor_soc_sim.sv").write_text(
        "module temp_monitor_soc_sim; endmodule\n",
        encoding="utf-8",
    )
    (system_dir / "temp_monitor_soc_phys.sv").write_text(
        "module temp_monitor_soc_phys; endmodule\n",
        encoding="utf-8",
    )

    sim_rel, sim_abs, _phys_rel, _phys_abs = agent._build_system_filelists(
        workflow_dir=str(tmp_path),
        existing_rtl=[],
        discovered_rtl=[
            "rtl/temp_monitor_digital.v",
            "analog/temp_sensor_adc_model.v",
            "analog/temp_sensor_adc_model_pass1.v",
        ],
        sim_top_rel="system/integration/temp_monitor_soc_sim.sv",
        phys_top_rel="system/integration/temp_monitor_soc_phys.sv",
    )

    assert "analog/temp_sensor_adc_model.v" in sim_rel
    assert "analog/temp_sensor_adc_model_pass1.v" not in sim_rel
    assert sum(1 for path in sim_abs if path.endswith("temp_sensor_adc_model.v")) == 1


def test_collect_module_files_prefers_final_model_over_pass_artifact(tmp_path):
    analog_dir = tmp_path / "analog"
    analog_dir.mkdir()
    (analog_dir / "ana_pass1.v").write_text("module ana; endmodule\n", encoding="utf-8")
    (analog_dir / "ana.v").write_text("module ana; endmodule\n", encoding="utf-8")

    assert agent._collect_module_files(str(tmp_path)) == ["analog/ana.v"]


def test_assemble_top_closes_missing_ports_and_input_to_input_connection():
    intent = {
        "instances": [
            {"name": "u_digital", "module": "digital_core"},
            {"name": "u_analog", "module": "analog_macro"},
        ],
        "connections": [
            {"from": "top.clk", "to": "u_digital.clk"},
            {"from": "u_digital.sample_req", "to": "u_analog.sample_req"},
            {"from": "u_analog.adc_code", "to": "u_digital.adc_code"},
        ],
        "tieoffs": [],
    }
    module_port_db = {
        "digital_core": {
            "clk": {"dir": "input", "range": ""},
            "sample_req": {"dir": "input", "range": ""},
            "adc_code": {"dir": "input", "range": "[11:0]"},
            "done": {"dir": "output", "range": ""},
        },
        "analog_macro": {
            "sample_req": {"dir": "input", "range": ""},
            "sensor_temp_celsius": {"dir": "input", "range": "[15:0]"},
            "adc_code": {"dir": "output", "range": "[11:0]"},
            "adc_valid": {"dir": "output", "range": ""},
        },
    }

    code = agent._assemble_top("soc_top_phys", intent, "phys", module_port_db)

    assert "input logic sample_req" in code
    assert "input logic [15:0] sensor_temp_celsius" in code
    assert ".sample_req(sample_req)" in code
    assert ".sensor_temp_celsius(sensor_temp_celsius)" in code
    assert "w_1_u_digital_sample_req__u_analog_sample_req" not in code
    assert "logic unused_u_analog_adc_valid;" in code
    assert ".adc_valid(unused_u_analog_adc_valid)" in code
