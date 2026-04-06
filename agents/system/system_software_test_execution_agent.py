import datetime
import json
import os
import shutil
import subprocess
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Test Execution Agent"
OUTPUT_SUBDIR = "system/software_validation/test"

REPORT_JSON = "test_execution_report.json"
SUMMARY_MD = "test_execution_summary.md"
DEBUG_JSON = "test_execution_debug.json"


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


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if isinstance(text, str) else ""


def _find_cargo() -> str:
    return shutil.which("cargo") or ""


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
            "stdout": _tail(result.stdout),
            "stderr": _tail(result.stderr),
        }
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
        }


def _resolve_test_root(state: Dict[str, Any]) -> str:
    explicit = state.get("system_software_build_root")
    if isinstance(explicit, str) and explicit.strip() and os.path.isdir(explicit.strip()):
        return explicit.strip()

    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered = validation_manifest.get("discovered_assets") or {}
    build_info = discovered.get("build_manifest") or {}

    resolved_build_manifest_path = str(build_info.get("resolved_path") or "").strip()
    if resolved_build_manifest_path:
        candidate = os.path.dirname(resolved_build_manifest_path)
        if os.path.isdir(candidate):
            return candidate

    workflow_dir = str(state.get("workflow_dir") or "").strip()
    if workflow_dir:
        fallback = os.path.join(workflow_dir, "system/software/build")
        if os.path.isdir(fallback):
            return fallback

    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    print(f"\n🧪 Running {AGENT_NAME}")

    test_manifest = state.get("system_software_test_manifest") or {}
    test_root = _resolve_test_root(state)

    if not test_root:
        debug = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "reason": "test_root_missing",
            "workflow_dir": state.get("workflow_dir") or "",
            "resolved_build_manifest_path": (
                ((state.get("system_software_validation_manifest") or {})
                 .get("discovered_assets") or {})
                .get("build_manifest", {})
                .get("resolved_path", "")
            ),
        }
        _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))
        state["status"] = "❌ test root missing"
        return state

    if not test_manifest:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_root": test_root,
            "test_status": "not_present",
            "message": "No test manifest found",
        }
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record(workflow_id, SUMMARY_MD, "# Test Execution Summary\n\n- Status: **not_present**\n")
        _record(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_root": test_root,
            "reason": "test_manifest_missing",
        }, indent=2))
        state["system_software_test_execution"] = report
        state["test_status"] = "not_present"
        state["status"] = "⚠️ no tests present"
        return state

    cargo_bin = _find_cargo()
    if not cargo_bin:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_root": test_root,
            "test_status": "environment_missing",
            "message": "cargo not found on PATH",
        }
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record(
            workflow_id,
            SUMMARY_MD,
            "# Test Execution Summary\n\n"
            "- Status: **environment_missing**\n"
            f"- Test root: `{test_root}`\n"
            "- Message: `cargo not found on PATH`\n",
        )
        _record(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_root": test_root,
            "cargo_bin": cargo_bin,
            "PATH": os.environ.get("PATH", ""),
        }, indent=2))
        state["system_software_test_execution"] = report
        state["test_status"] = "fail"
        state["status"] = "⚠️ test environment missing"
        return state

    result = _run_cmd([cargo_bin, "test", "--workspace"], test_root)
    test_status = "pass" if result["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "test_root": test_root,
        "cargo_bin": cargo_bin,
        "returncode": result["returncode"],
        "test_status": test_status,
        "stdout_tail": result["stdout"],
        "stderr_tail": result["stderr"],
    }

    summary = (
        "# Test Execution Summary\n\n"
        f"- Status: **{test_status}**\n"
        f"- Test root: `{test_root}`\n"
        f"- Cargo: `{cargo_bin}`\n"
        f"- Return code: `{result['returncode']}`\n"
    )

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)
    _record(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "test_root": test_root,
        "cargo_bin": cargo_bin,
    }, indent=2))

    state["system_software_test_execution"] = report
    state["test_status"] = test_status
    state["status"] = "✅ tests passed" if test_status == "pass" else "⚠️ tests failed"
    return state