import json
import os


os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_pd_signoff_closure_agent as digital_agent
from agents.digital import digital_synthesis_closure_agent as synthesis_agent
from agents.system import system_pd_signoff_closure_agent as system_agent


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_signoff_closure_selects_floorplan_for_tap_drc_and_bundles_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "drc" / "drc_summary.json", {
        "status": "violations_found",
        "drc_violations": 3,
    })
    report_dir = tmp_path / "digital" / "drc" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "drc.xml").write_text(
        """
<report-database>
  <items>
    <item><category>difftap.3</category></item>
    <item><category>difftap.3</category></item>
    <item><category>licon.2</category></item>
  </items>
</report-database>
""",
        encoding="utf-8",
    )
    _write_json(tmp_path / "digital" / "sta_postfill" / "metrics.json", {
        "worst_slack": -0.12,
        "tns": -1.0,
        "setup_violations": 4,
    })

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
        "max_signoff_closure_iterations": 2,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["status"] == "planned"
    assert plan["selected_restart_stage"] == "Digital Floorplan Agent"
    assert plan["dominant_issue"] == "digital_drc"
    assert plan["max_iterations"] == 2
    fill_overrides = plan["eco_profile"]["config_overrides"]["fill"]
    assert fill_overrides["RUN_FILL_INSERTION"] is True
    assert fill_overrides["CHIPLOOP_FILL_DRC_REPAIR"] == "tap_contact_fill_spacing_iter_1"
    assert plan["eco_profile"]["config_overrides"]["route"]["CHIPLOOP_TIMING_CLOSURE_ECO"] == "setup_iter_1"
    assert any(action["type"] == "setup_timing" for action in plan["repair_actions"])
    assert not any(item["type"] == "setup_timing" for item in plan["skipped_repairs"])
    chart = out["digital"]["signoff_closure"]["chart"]
    assert chart["baseline_only"] is True
    assert chart["series"][0]["drc_violations"] == 3
    assert chart["series"][0]["postfill_wns"] == -0.12


def test_signoff_closure_routes_macro_local_drc_to_analog_collateral(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "drc" / "drc_summary.json", {
        "status": "violations_found",
        "drc_violations": 2,
    })
    report_dir = tmp_path / "digital" / "drc" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "drc.xml").write_text(
        """
<report-database>
  <top-cell>soc_top</top-cell>
  <items>
    <item><category>licon.2</category><cell>ana_macro</cell></item>
    <item><category>li.3</category><cell>ana_macro</cell></item>
  </items>
</report-database>
""",
        encoding="utf-8",
    )

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["dominant_issue"] == "digital_drc"
    assert plan["selected_restart_stage"] == "Analog Physical Collateral Package Agent"
    assert plan["issue_summary"][0]["evidence"]["cell_counts"] == {"ana_macro": 2}
    assert plan["eco_profile"]["enabled"] is False
    assert plan["eco_profile"]["reason"] == "macro_local_drc_requires_analog_collateral_repair"


def test_digital_pd_signoff_closure_reports_synthesis_lec_outside_pd_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "drc" / "drc_summary.json", {"status": "clean", "drc_violations": 0})
    _write_json(tmp_path / "digital" / "lvs" / "lvs_summary.json", {"status": "clean"})
    _write_json(tmp_path / "digital" / "tapeout" / "tapeout_summary.json", {"status": "ok"})
    _write_json(tmp_path / "digital" / "lec" / "lec_summary.json", {
        "status": "inconclusive",
        "failure_reason": "missing SAT models",
        "unproven_points": 7,
    })

    out = digital_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
        "allow_lec_repair": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["agent"] == "Digital PD Signoff Closure Agent"
    assert plan["dominant_issue"] == "synthesis_lec"
    assert plan["selected_restart_stage"] is None
    assert not any(action["type"] == "synthesis_lec" for action in plan["repair_actions"])
    assert any(
        item["type"] == "synthesis_lec" and "Outside PD signoff closure scope" in item["reason"]
        for item in plan["skipped_repairs"]
    )


def test_signoff_closure_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    out = digital_agent.run_agent({"workflow_id": "wf", "workflow_dir": str(tmp_path)})

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["enabled"] is False
    assert plan["status"] == "skipped"


def test_signoff_closure_routes_blackbox_lvs_to_analog_gds_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "analog" / "physical_package" / "analog_physical_collateral_package.json", {
        "status": "ready",
        "mode": "blackbox",
        "blackbox_for_drc_lvs": True,
        "blackbox_reason": "analog_macro_gds_missing",
        "gds": "",
    })
    _write_json(tmp_path / "digital" / "drc" / "drc_summary.json", {
        "status": "blackbox_deferred",
        "drc_status": "blackbox_deferred",
        "reason": "analog_macro_gds_missing",
    })
    _write_json(tmp_path / "digital" / "lvs" / "lvs_summary.json", {
        "status": "blackbox_deferred",
        "lvs_status": "blackbox_deferred",
        "reason": "analog_macro_gds_missing",
    })

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["dominant_issue"] == "analog_macro_gds_missing"
    assert plan["selected_restart_stage"] == "Analog Physical Collateral Package Agent"
    assert not any(item["type"] == "digital_lvs" for item in plan["issue_summary"])


def test_signoff_closure_routes_macro_bus_lvs_to_lvs_staging_and_defers_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "lvs" / "lvs_summary.json", {
        "status": "mismatch",
        "lvs_status": "mismatch",
        "failure_reason": "macro_bus_width_mismatch",
    })
    _write_json(tmp_path / "digital" / "sta_postfill" / "metrics.json", {
        "worst_slack": -0.006,
        "tns": -0.006,
        "setup_violations": 1,
    })

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["dominant_issue"] == "digital_lvs"
    assert plan["selected_restart_stage"] == "Digital LVS Agent"
    assert "route" not in plan["eco_profile"]["config_overrides"]
    assert any("Timing is not the dominant restart cause" in note for note in plan["eco_profile"]["notes"])


def test_signoff_closure_bundles_timing_when_lvs_restart_is_physical(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "lvs" / "lvs_summary.json", {
        "status": "mismatch",
        "lvs_status": "mismatch",
        "failure_reason": "top_level_pin_mismatch",
    })
    _write_json(tmp_path / "digital" / "sta_postfill" / "metrics.json", {
        "worst_slack": -0.04,
        "tns": -0.12,
        "setup_violations": 3,
    })

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["dominant_issue"] == "digital_lvs"
    assert plan["selected_restart_stage"] == "Digital Floorplan Agent"
    assert plan["eco_profile"]["config_overrides"]["route"]["CHIPLOOP_TIMING_CLOSURE_ECO"] == "setup_iter_1"
    assert any(action["type"] == "setup_timing" for action in plan["repair_actions"])


def test_signoff_closure_setup_timing_restart_has_placement_eco(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "floorplan" / "config.json", {
        "PL_TARGET_DENSITY": 0.25,
    })
    _write_json(tmp_path / "digital" / "sta_postfill" / "metrics.json", {
        "worst_slack": -0.08,
        "tns": -0.44,
        "setup_violations": 5,
        "hold_violations": 0,
    })

    out = system_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    overrides = plan["eco_profile"]["config_overrides"]
    assert plan["dominant_issue"] == "setup_timing"
    assert plan["selected_restart_stage"] == "Digital Placement Agent"
    assert overrides["placement"]["CHIPLOOP_TIMING_CLOSURE_ECO"] == "setup_iter_1"
    assert overrides["placement"]["PL_TARGET_DENSITY"] < 0.25
    assert overrides["placement"]["PL_RESIZER_TIMING_OPTIMIZATIONS"] is True
    assert overrides["route"]["GRT_RESIZER_TIMING_OPTIMIZATIONS"] is True


def test_signoff_closure_treats_bounded_synthesis_lec_as_advisory_when_tapeout_lec_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "drc" / "drc_summary.json", {"status": "clean", "drc_violations": 0})
    _write_json(tmp_path / "digital" / "lvs" / "lvs_summary.json", {"status": "clean"})
    _write_json(tmp_path / "digital" / "tapeout" / "tapeout_summary.json", {"status": "ok"})
    _write_json(tmp_path / "digital" / "tapeout_lec" / "tapeout_lec_summary.json", {"status": "pass"})
    _write_json(tmp_path / "digital" / "sta_postfill" / "metrics.json", {
        "worst_slack": 2.3,
        "tns": 0,
        "setup_violations": 0,
        "hold_violations": 0,
    })
    _write_json(tmp_path / "digital" / "lec" / "lec_summary.json", {
        "status": "inconclusive_bounded_sequential_proof_unproven",
        "failure_reason": "bounded_sequential_equivalence_points_unproven",
        "unproven_points": 3,
        "unproven_equiv_points": ["counter_value[0]", "pwm_out", "rd_data[0]"],
    })

    out = digital_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_signoff_closure_loop": True,
    })

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["status"] == "clean"
    assert plan["closure_complete"] is True
    assert plan["dominant_issue"] is None
    assert plan["repair_actions"] == []
    assert plan["advisories"][0]["type"] == "synthesis_lec_bounded_warning"
    assert plan["advisories"][0]["evidence"]["unproven_equiv_points"] == ["counter_value[0]", "pwm_out", "rd_data[0]"]


def test_synthesis_closure_selects_synthesis_for_setup_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "sta_preplace" / "metrics.json", {
        "worst_slack": -0.25,
        "tns": -3.5,
        "setup_violations": 12,
    })
    _write_json(tmp_path / "digital" / "lec" / "lec_summary.json", {"status": "pass"})

    out = synthesis_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_synthesis_closure_loop": True,
        "max_synthesis_closure_iterations": 2,
    })

    plan = out["digital"]["synthesis_closure"]["plan"]
    assert plan["status"] == "planned"
    assert plan["dominant_issue"] == "setup_timing"
    assert plan["selected_restart_stage"] == "Digital Synthesis Agent"
    assert plan["max_iterations"] == 2
    assert plan["downstream_policy"]["continue_downstream_pd"] is True
    assert plan["downstream_policy"]["stop_on_synthesis_closure_failure"] is False
    chart = out["digital"]["synthesis_closure"]["chart"]
    assert chart["baseline_only"] is True
    assert chart["series"][0]["wns"] == -0.25
    assert chart["series"][0]["setup_violations"] == 12


def test_synthesis_closure_treats_bounded_lec_as_advisory_when_post_dft_lec_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "sta_preplace" / "metrics.json", {
        "worst_slack": 1.25,
        "tns": 0,
        "setup_violations": 0,
    })
    _write_json(tmp_path / "digital" / "dft" / "scan_summary.json", {"status": "scan_replace_pass"})
    _write_json(tmp_path / "digital" / "post_dft_lec" / "post_dft_lec_summary.json", {
        "status": "pass",
        "failure_reason": "equivalence_proven",
    })
    _write_json(tmp_path / "digital" / "lec" / "lec_summary.json", {
        "status": "inconclusive_bounded_sequential_proof_unproven",
        "failure_reason": "bounded_sequential_equivalence_points_unproven",
        "unproven_points": 3,
        "unproven_equiv_points": ["counter_value[0]", "pwm_out", "rd_data[0]"],
    })

    out = synthesis_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_synthesis_closure_loop": True,
        "stop_on_synthesis_closure_failure": True,
        "stop_on_synthesis_lec_failure": True,
    })

    plan = out["digital"]["synthesis_closure"]["plan"]
    assert plan["status"] == "clean"
    assert plan["closure_complete"] is True
    assert plan["dominant_issue"] is None
    assert plan["repair_actions"] == []
    assert plan["advisories"][0]["type"] == "synthesis_lec_bounded_warning"
    assert plan["downstream_policy"]["continue_downstream_pd"] is True


def test_synthesis_closure_treats_reset_sequence_repaired_lec_as_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesis_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    _write_json(tmp_path / "digital" / "sta_preplace" / "metrics.json", {
        "worst_slack": 1.0,
        "tns": 0,
        "setup_violations": 0,
    })
    _write_json(tmp_path / "digital" / "lec" / "lec_summary.json", {
        "status": "pass",
        "failure_reason": "equivalence_proven",
        "proof_strategy": "reset_sequence_repair",
        "primary_status": "inconclusive_bounded_sequential_proof_unproven",
        "primary_unproven_points": 3,
        "unproven_points": 0,
        "reset_sequence_repair": {"status": "pass"},
    })

    out = synthesis_agent.run_agent({
        "workflow_id": "wf",
        "workflow_dir": str(tmp_path),
        "run_synthesis_closure_loop": True,
    })

    plan = out["digital"]["synthesis_closure"]["plan"]
    assert plan["status"] == "clean"
    assert plan["closure_complete"] is True
    assert plan["issue_summary"] == []
