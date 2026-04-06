import datetime
import json
import os
import subprocess
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Build Validation Agent"
OUTPUT_SUBDIR = "system/software_validation/build"

REPORT_JSON = "build_validation_report.json"
SUMMARY_MD = "build_validation_summary.md"


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

    print(f"\n⚙️ Running {AGENT_NAME}")

    build_manifest = state.get("system_software_build_manifest") or {}
    build_root = os.path.join(workflow_dir, "system/software/build")

    if not os.path.isdir(build_root):
        state["status"] = "❌ build root missing"
        return state

    result = _run_cmd(["cargo", "build", "--workspace"], build_root)
    build_status = "pass" if result["returncode"] == 0 else "fail"

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "build_root": build_root,
        "workspace_members": build_manifest.get("workspace_members") or [],
        "returncode": result["returncode"],
        "build_status": build_status,
        "stdout_tail": result["stdout"],
        "stderr_tail": result["stderr"],
    }

    summary = f"# Build Validation Summary\n\n- Status: **{build_status}**\n- Return code: {result['returncode']}\n"

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)

    state["system_software_build_validation"] = report
    state["build_status"] = build_status
    state["status"] = "✅ build validation complete" if build_status == "pass" else "⚠️ build failed"
    return state
