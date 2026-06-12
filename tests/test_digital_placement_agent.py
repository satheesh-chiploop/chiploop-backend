import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_placement_agent as agent


def test_macro_placement_cfg_generated_from_netlist_and_lef(tmp_path):
    netlist = tmp_path / "temp_monitor_soc_phys_synth.v"
    lef = tmp_path / "temp_sensor_adc_model.lef"
    work_stage = tmp_path / "run_work" / "place"

    netlist.write_text(
        "module temp_monitor_soc_phys;\n"
        "  temp_sensor_adc_model u_analog (.adc_valid(adc_valid));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    lef.write_text("MACRO temp_sensor_adc_model\nEND temp_sensor_adc_model\n", encoding="utf-8")

    cfg = {"DIE_AREA": "0 0 120 120"}
    path = agent._write_macro_placement_cfg_if_needed(
        cfg=cfg,
        work_stage_dir=str(work_stage),
        stage_netlists=[str(netlist)],
        macro_lef_paths=[str(lef)],
    )

    assert path is not None
    assert cfg["MACRO_PLACEMENT_CFG"] == "dir::inputs/macros/macro_placement.cfg"
    assert "u_analog 18.000 18.000 N" in open(path, encoding="utf-8").read()


def test_stage_macro_inputs_dedupes_duplicate_collateral(tmp_path):
    lef = tmp_path / "ana.lef"
    lib = tmp_path / "ana.lib"
    lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    lib.write_text("library (ana) {}\n", encoding="utf-8")

    staged_lefs, staged_libs, staged_gds = agent._stage_macro_inputs(
        {"digital": {"macro_lefs": [str(lef), str(lef)], "macro_libs": [str(lib), str(lib)], "macro_gds": []}},
        str(tmp_path),
        str(tmp_path / "stage"),
    )

    assert staged_lefs == ["dir::inputs/macros/lef/ana.lef"]
    assert staged_libs == ["dir::inputs/macros/lib/ana.lib"]
    assert staged_gds == []
