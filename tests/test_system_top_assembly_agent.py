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
