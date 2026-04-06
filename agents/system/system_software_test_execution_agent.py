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
    candidates = []

    explicit = state.get("system_software_build_root")
    if isinstance(explicit, str) and explicit.strip():
        candidates.append(explicit.strip())

    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered = validation_manifest.get("discovered_assets") or {}

    for asset_key in ["test_manifest", "build_manifest", "package_manifest", "workspace_manifest"]:
        info = discovered.get(asset_key) or {}
        resolved_path = str(info.get("resolved_path") or "").strip()
        if resolved_path:
            candidates.append(os.path.dirname(resolved_path))

    workflow_dir = str(state.get("workflow_dir") or "").strip()
    if workflow_dir:
        candidates.extend([
            os.path.join(workflow_dir, "system/software/build"),
            os.path.join(workflow_dir, "system/software"),
            os.path.join(workflow_dir, "system"),
        ])

    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "Cargo.toml")):
            return candidate

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            for root, _, files in os.walk(candidate):
                if "Cargo.toml" in files:
                    return root

    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    print(f"\n🧪 Running {AGENT_NAME}")

    test_manifest = state.get("system_software_test_manifest") or {}
    test_root = _resolve_test_root(state)

    if not test_root:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "test_root": "",
            "test_status": "not_present",
            "message": "No Cargo workspace/test root could be resolved.",
        }
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
        _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record(workflow_id, SUMMARY_MD, "# Test Execution Summary\n\n- Status: **not_present**\n- Message: `No Cargo workspace/test root could be resolved.`\n")
        _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))
        state["system_software_test_execution"] = report
        state["test_status"] = "not_present"
        state["status"] = "⚠️ test root not present"
        return state

    manifest_missing = not bool(test_manifest)

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
        state["test_status"] = "environment_missing"
        state["status"] = "⚠️ test environment missing"
        return state

    attempts = []
    commands = [
        [cargo_bin, "test", "--workspace"],
        [cargo_bin, "test"],
    ]

    selected = None
    for cmd in commands:
        result = _run_cmd(cmd, test_root)
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
        "stderr_tail": "No test command candidates were generated.",
    })

    test_status = "pass" if final_attempt["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "test_root": test_root,
        "cargo_bin": cargo_bin,
        "test_manifest_present": not manifest_missing,
        "selected_command": final_attempt["command"],
        "returncode": final_attempt["returncode"],
        "test_status": test_status,
        "stdout_tail": final_attempt["stdout_tail"],
        "stderr_tail": final_attempt["stderr_tail"],
        "attempt_count": len(attempts),
    }

    summary = (
        "# Test Execution Summary\n\n"
        f"- Status: **{test_status}**\n"
        f"- Test root: `{test_root}`\n"
        f"- Test manifest present: `{not manifest_missing}`\n"
        f"- Cargo: `{cargo_bin}`\n"
        f"- Command: `{ ' '.join(final_attempt['command']) }`\n"
        f"- Return code: `{final_attempt['returncode']}`\n"
    )

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)
    _record(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "test_root": test_root,
        "cargo_bin": cargo_bin,
        "attempts": attempts,
    }, indent=2))

    state["system_software_test_execution"] = report
    state["test_status"] = test_status
    state["status"] = "✅ tests passed" if test_status == "pass" else "⚠️ tests failed"
    return state
