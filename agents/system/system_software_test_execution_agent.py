import datetime
import json
import os
import subprocess
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Test Execution Agent"
OUTPUT_SUBDIR = "system/software_validation/test"

REPORT_JSON = "test_execution_report.json"
SUMMARY_MD = "test_execution_summary.md"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id, filename, content):
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _run_cmd(cmd, cwd):
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        }


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir") or ""

    print(f"\n🧪 Running {AGENT_NAME}")

    test_manifest = state.get("system_software_test_manifest") or {}
    test_root = os.path.join(workflow_dir, "system/software/build")

    if not os.path.isdir(test_root):
        state["status"] = "❌ test root missing"
        return state

    if not test_manifest:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_status": "not_present"
        }
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        state["test_status"] = "not_present"
        state["status"] = "⚠️ no tests present"
        return state

    result = _run_cmd(["cargo", "test", "--workspace"], test_root)
    test_status = "pass" if result["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "test_status": test_status,
        "stdout_tail": result["stdout"],
        "stderr_tail": result["stderr"],
    }

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, f"# Test Status: {test_status}")

    state["system_software_test_execution"] = report
    state["test_status"] = test_status
    state["status"] = "✅ tests passed" if test_status == "pass" else "⚠️ tests failed"
    return state
