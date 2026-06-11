import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_closure_recommendation_agent as recommendation_agent
from agents.digital import digital_coverage_gap_analysis_agent as gap_agent
from agents.digital import digital_failure_triage_agent as triage_agent
from agents.digital import digital_testcase_seed_update_agent as testcase_seed_agent
from agents.digital import digital_verify_closure_ingest_agent as ingest_agent


def _stub_upload(monkeypatch):
    monkeypatch.setattr(ingest_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(gap_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(triage_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(recommendation_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(testcase_seed_agent, "save_text_artifact_and_record", lambda *args, **kwargs: None)


def test_verify_closure_agents_generate_plan_from_parent_verify_artifacts(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    monkeypatch.chdir(tmp_path)

    source_id = "verify-parent"
    source_reports = tmp_path / "backend" / "workflows" / source_id / "vv" / "tb" / "reports"
    run_logs = source_reports / "run_logs"
    run_logs.mkdir(parents=True)
    (source_reports / "simulation_summary_coverage.json").write_text(
        json.dumps(
            {
                "simulation": {"total": 2, "pass": 1, "fail": 1},
                "coverage": {
                    "functional_coverage_pct": 75.0,
                    "functional": {"coverage_pct": 75.0},
                    "code": {"line_coverage_pct": 70.0, "branch_coverage_pct": 50.0},
                },
            }
        ),
        encoding="utf-8",
    )
    (source_reports / "simulation_execution_summary.json").write_text(
        json.dumps(
            {
                "total": 2,
                "pass": 1,
                "fail": 1,
                "results": [
                    {"testcase": "smoke_test", "seed": 1, "pass": True, "rc": 0},
                    {"testcase": "constrained_random_sanity", "seed": 2, "pass": False, "rc": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    (source_reports / "functional_coverage_summary.json").write_text(
        json.dumps(
            {
                "outputs": {
                    "irq": {"hit_bins": 1, "total_bins": 2, "seen_values": [0]},
                }
            }
        ),
        encoding="utf-8",
    )
    (run_logs / "constrained_random_sanity__seed_2.stderr.log").write_text(
        "Assertion failed at pwm_controller.sv:88\n",
        encoding="utf-8",
    )

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_verify_workflow_id": source_id,
        "coverage_targets": "100% functional, 90% line, 80% branch",
        "seed_count": 4,
    }

    ingest_agent.run_agent(state)
    gap_agent.run_agent(state)
    triage_agent.run_agent(state)
    recommendation_agent.run_agent(state)

    plan_path = Path(state["workflow_dir"]) / "verify_closure" / "verify_closure_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["verdict"] == "debug_failures_first"
    assert plan["coverage_gap_count"] >= 1
    assert plan["functional_gap_count"] == 1
    assert plan["functional_gaps"][0]["coverage_point"] == "outputs.irq"
    assert plan["functional_gaps"][0]["missing_bins"] == ["nonzero"]
    assert plan["failure_count"] == 1
    assert any(item["id"] == "rerun_failed_seeds_with_waveform" for item in plan["recommended_actions"])


def test_system_sim_closure_updates_system_specific_seed_keys(tmp_path, monkeypatch):
    _stub_upload(monkeypatch)
    monkeypatch.chdir(tmp_path)

    state = {
        "workflow_id": "closure-child",
        "workflow_dir": str(tmp_path / "backend" / "workflows" / "closure-child"),
        "source_system_sim_workflow_id": "system-parent",
        "source_simulation_manifest": {
            "top_module": "temp_monitor_soc_sim",
            "default_tests": ["system_smoke_test", "integrated_input_sanity"],
        },
        "closure_added_coverage_points": [
            {"id": "COV_ITER001_001", "source_gap_type": "functional_bin_gap", "coverage_point": "outputs.alert_irq"}
        ],
        "seed_budget": 4,
        "random_vs_directed": "both",
    }

    testcase_seed_agent.run_agent(state)

    assert state["system_sim_testcases"] == ["system_smoke_test"]
    assert state["system_sim_seeds"] == [1, 2, 3, 4]
    assert state["simulation_seeds"] == [1, 2, 3, 4]
