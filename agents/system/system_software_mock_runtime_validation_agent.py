
import datetime
import json
import os
import subprocess
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Mock Runtime Validation Agent"
OUTPUT_SUBDIR = "system/software_validation/mock"

REPORT_JSON = "mock_runtime_validation_report.json"
SUMMARY_MD = "mock_runtime_validation_summary.md"
DEBUG_JSON = "mock_runtime_validation_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
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


def _run_cmd(cmd: List[str], cwd: str, timeout_sec: int = 600) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
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


def _candidate_mock_targets(mock_manifest: Dict[str, Any]) -> List[List[str]]:
    candidates: List[List[str]] = []

    explicit = mock_manifest.get("commands") or []
    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, list) and item:
                candidates.append([str(x) for x in item])
            elif _is_nonempty_str(item):
                candidates.append([str(item)])

    # deterministic fallbacks
    candidates.extend([
        ["cargo", "test", "--workspace", "--features", "mock"],
        ["cargo", "test", "--workspace"],
    ])

    seen = set()
    deduped: List[List[str]] = []
    for cmd in candidates:
        key = tuple(cmd)
        if key not in seen:
            seen.add(key)
            deduped.append(cmd)
    return deduped


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir") or "")

    print(f"\n🧪 Running {AGENT_NAME}")

    validation_manifest = state.get("system_software_validation_manifest") or {}
    mock_manifest = state.get("system_software_mock_manifest") or {}

    build_root = os.path.join(workflow_dir, "system/software/build")
    mock_root = os.path.join(workflow_dir, "system/software/mock")

    if not os.path.isdir(build_root):
        state["status"] = "❌ mock runtime validation build root missing"
        return state

    if not mock_manifest and not os.path.isdir(mock_root):
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "validation_scope": "software_only",
            "mock_runtime_status": "not_present",
            "message": "No mock runtime manifest or mock runtime directory found.",
        }
        _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record_text(workflow_id, SUMMARY_MD, "# Mock Runtime Validation\n\n- Status: **not_present**\n")
        _record_text(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "build_root": build_root,
            "mock_root": mock_root,
            "reason": "mock_manifest_and_directory_missing",
        }, indent=2))
        state["system_software_mock_runtime_validation"] = report
        state["mock_runtime_status"] = "not_present"
        state["status"] = "⚠️ mock runtime not present"
        return state

    attempts = []
    selected = None
    for cmd in _candidate_mock_targets(mock_manifest):
        result = _run_cmd(cmd, cwd=build_root)
        attempts.append({
            "command": cmd,
            "returncode": result["returncode"],
            "stdout_tail": result["stdout"],
            "stderr_tail": result["stderr"],
        })
        if result["returncode"] == 0:
            selected = attempts[-1]
            break

    final_attempt = selected or (attempts[-1] if attempts else {
        "command": [],
        "returncode": -1,
        "stdout_tail": "",
        "stderr_tail": "No command candidates were generated.",
    })

    mock_runtime_status = "pass" if final_attempt["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "validation_scope": "software_only",
        "co_sim_enabled": False,
        "build_root": build_root,
        "mock_root": mock_root,
        "mock_runtime_status": mock_runtime_status,
        "selected_command": final_attempt["command"],
        "returncode": final_attempt["returncode"],
        "stdout_tail": final_attempt["stdout_tail"],
        "stderr_tail": final_attempt["stderr_tail"],
        "attempt_count": len(attempts),
    }

    summary = (
        "# Mock Runtime Validation\n\n"
        f"- Status: **{mock_runtime_status}**\n"
        f"- Command: `{ ' '.join(final_attempt['command']) }`\n"
        f"- Return code: `{final_attempt['returncode']}`\n"
    )

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "validation_manifest_present": bool(validation_manifest),
        "mock_manifest_present": bool(mock_manifest),
        "attempts": attempts,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, summary)
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_mock_runtime_validation"] = report
    state["mock_runtime_status"] = mock_runtime_status
    state["status"] = "✅ mock runtime validation complete" if mock_runtime_status == "pass" else "⚠️ mock runtime validation failed"
    return state
