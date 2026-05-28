import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_validation_summary_l2_agent as agent


def test_summary_preserves_not_applicable_scenario_count(monkeypatch):
    monkeypatch.setattr(agent, "_record_text", lambda *args, **kwargs: None)

    state = {
        "workflow_id": "wf-test",
        "system_software_cosim_harness_manifest": {"harness_status": "ready", "l1_ready": None},
        "system_software_cosim_execution_report": {
            "execution_status": "pass",
            "scenario_count": 6,
            "scenario_blocked_count": 0,
        },
        "system_software_cosim_trace_validation_report": {
            "trace_validation_status": "pass",
            "scenario_count": 6,
            "scenario_pass_count": 4,
            "scenario_fail_count": 0,
            "scenario_not_applicable_count": 2,
            "scenario_applicable_count": 4,
        },
    }

    result = agent.run_agent(state)
    summary = result["system_software_validation_summary_l2"]

    assert summary["final_system_correctness_verdict"] == "pass"
    assert summary["scenario_count"] == 6
    assert summary["scenario_pass_count"] == 4
    assert summary["scenario_not_applicable_count"] == 2
    assert summary["scenario_applicable_count"] == 4
