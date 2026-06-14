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


def test_signoff_closure_selects_floorplan_for_tap_drc_and_skips_later_timing(tmp_path, monkeypatch):
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
    assert any(item["type"] == "setup_timing" for item in plan["skipped_repairs"])
    chart = out["digital"]["signoff_closure"]["chart"]
    assert chart["baseline_only"] is True
    assert chart["series"][0]["drc_violations"] == 3
    assert chart["series"][0]["postfill_wns"] == -0.12


def test_digital_pd_signoff_closure_includes_lec_repair(tmp_path, monkeypatch):
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
    assert plan["selected_restart_stage"] == "Digital Synthesis Agent"
    assert any(action["type"] == "synthesis_lec" for action in plan["repair_actions"])


def test_signoff_closure_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(system_agent, "save_text_artifact_and_record", lambda *args, **kwargs: "local")

    out = digital_agent.run_agent({"workflow_id": "wf", "workflow_dir": str(tmp_path)})

    plan = out["digital"]["signoff_closure"]["plan"]
    assert plan["enabled"] is False
    assert plan["status"] == "skipped"


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
