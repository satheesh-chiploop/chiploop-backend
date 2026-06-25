import json
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_rtl_dashboard_agent as agent


def test_digital_lint_status_requires_dual_tool_evidence(tmp_path):
    digital = tmp_path / "digital"
    digital.mkdir()
    report = digital / "rtl_lint_report.json"

    report.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    assert agent._digital_lint_status(str(tmp_path)) == "not produced"

    report.write_text(
        json.dumps({
            "status": "pass",
            "icarus_compile": {"available": True, "returncode": 0, "status": "pass"},
            "verilator_lint": {
                "available": True,
                "returncode": 0,
                "status": "pass",
                "blocking_warning_codes": [],
            },
        }),
        encoding="utf-8",
    )
    assert agent._digital_lint_status(str(tmp_path)) == "pass"

    report.write_text(
        json.dumps({
            "status": "pass",
            "icarus_compile": {"available": True, "returncode": 1, "status": "fail"},
            "verilator_lint": {"available": True, "returncode": 0, "status": "pass"},
        }),
        encoding="utf-8",
    )
    assert agent._digital_lint_status(str(tmp_path)) == "fail"
