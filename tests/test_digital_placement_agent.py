import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.digital import digital_cts_agent as cts_agent
from agents.digital import digital_placement_agent as agent
from agents.digital import digital_sta_postplace_agent as postplace_agent


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


def test_latest_run_dir_finds_stage_local_openlane_runs(tmp_path):
    run_work = tmp_path / "run_work"
    old_run = run_work / "runs" / "old"
    place_run = run_work / "place" / "runs" / "System_PD_wf"
    old_run.mkdir(parents=True)
    place_run.mkdir(parents=True)
    os.utime(old_run, (1, 1))
    os.utime(place_run, (2, 2))

    assert agent._latest_run_dir(str(run_work)) == str(place_run)


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


def test_stage_macro_inputs_dedupes_collateral_with_same_staged_name(tmp_path):
    prior = tmp_path / "prior"
    generated = tmp_path / "generated"
    prior.mkdir()
    generated.mkdir()
    prior_lef = prior / "ana.lef"
    generated_lef = generated / "ana.lef"
    prior_lib = prior / "ana.lib"
    generated_lib = generated / "ana.lib"
    prior_lef.write_text("MACRO ana\nEND ana\n", encoding="utf-8")
    generated_lef.write_text("MACRO ana\n  PIN VPWR\n  END VPWR\nEND ana\n", encoding="utf-8")
    prior_lib.write_text("library (ana_prior) {}\n", encoding="utf-8")
    generated_lib.write_text("library (ana_generated) {}\n", encoding="utf-8")

    staged_lefs, staged_libs, staged_gds = agent._stage_macro_inputs(
        {
            "digital": {
                "macro_lefs": [str(prior_lef), str(generated_lef)],
                "macro_libs": [str(prior_lib), str(generated_lib)],
                "macro_gds": [],
            }
        },
        str(tmp_path),
        str(tmp_path / "stage"),
    )

    assert staged_lefs == ["dir::inputs/macros/lef/ana.lef"]
    assert staged_libs == ["dir::inputs/macros/lib/ana.lib"]
    assert staged_gds == []
    assert "PIN VPWR" in (tmp_path / "stage" / "inputs" / "macros" / "lef" / "ana.lef").read_text(encoding="utf-8")
    assert "ana_generated" in (tmp_path / "stage" / "inputs" / "macros" / "lib" / "ana.lib").read_text(encoding="utf-8")


def test_cts_stages_inherited_macro_placement_cfg(tmp_path):
    workflow_dir = tmp_path / "workflow"
    place_dir = workflow_dir / "digital" / "place"
    work_stage = tmp_path / "run_work" / "cts"
    place_dir.mkdir(parents=True)
    macro_cfg = place_dir / "macro_placement.cfg"
    macro_cfg.write_text("u_analog 18.000 18.000 N\n", encoding="utf-8")
    cfg = {"MACRO_PLACEMENT_CFG": "dir::inputs/macros/macro_placement.cfg"}

    staged = cts_agent._stage_macro_placement_cfg_if_needed(
        cfg,
        {"digital": {"place": {"macro_placement_cfg": str(macro_cfg)}}},
        str(workflow_dir),
        str(work_stage),
    )

    assert cfg["MACRO_PLACEMENT_CFG"] == "dir::inputs/macros/macro_placement.cfg"
    assert staged == str(work_stage / "inputs" / "macros" / "macro_placement.cfg")
    assert "u_analog" in (work_stage / "inputs" / "macros" / "macro_placement.cfg").read_text(encoding="utf-8")


def test_macro_placement_cfg_staging_is_idempotent_when_already_local(tmp_path):
    work_stage = tmp_path / "run_work" / "place"
    local_cfg = work_stage / "inputs" / "macros" / "macro_placement.cfg"
    local_cfg.parent.mkdir(parents=True)
    local_cfg.write_text("u_analog 18.000 18.000 N\n", encoding="utf-8")
    cfg = {"MACRO_PLACEMENT_CFG": "dir::inputs/macros/macro_placement.cfg"}

    staged = postplace_agent._stage_macro_placement_cfg_if_needed(
        cfg,
        {"digital": {"place": {"macro_placement_cfg": str(local_cfg)}}},
        str(tmp_path / "workflow"),
        str(work_stage),
    )

    assert staged == str(local_cfg)
    assert cfg["MACRO_PLACEMENT_CFG"] == "dir::inputs/macros/macro_placement.cfg"
    assert local_cfg.read_text(encoding="utf-8").startswith("u_analog")
