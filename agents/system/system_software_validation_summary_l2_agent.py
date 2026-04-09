import datetime
import json
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Validation Summary (L2)"
OUTPUT_SUBDIR = "system/software_validation/cosim/summary"
REPORT_JSON = "system_software_validation_summary_l2.json"
SUMMARY_MD = "system_software_validation_summary_l2.md"
DEBUG_JSON = "system_software_validation_summary_l2_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _status_of(obj: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    harness = state.get("system_software_cosim_harness_manifest") or {}
    execution = state.get("system_software_cosim_execution_report") or {}
    trace = state.get("system_software_cosim_trace_validation_report") or {}

    harness_status = _status_of(harness, "harness_status")
    execution_status = _status_of(execution, "execution_status")
    trace_status = _status_of(trace, "trace_validation_status")

    scenario_count = int(trace.get("scenario_count") or execution.get("scenario_count") or 0)
    pass_count = int(trace.get("scenario_pass_count") or 0)
    fail_count = int(trace.get("scenario_fail_count") or 0)
    blocked_count = int(execution.get("scenario_blocked_count") or 0)

    if harness_status == "blocked":
        final_verdict = "blocked"
    elif trace_status == "pass":
        final_verdict = "pass"
    elif trace_status == "partial_pass" or execution_status == "partial_pass":
        final_verdict = "partial_pass"
    elif trace_status == "fail" or execution_status == "fail":
        final_verdict = "fail"
    else:
        final_verdict = "blocked"

    validation_report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "validation_scope": "l2_cosim",
        "co_sim_enabled": True,
        "l1_ready": bool(harness.get("l1_ready")),
        "harness_status": harness_status,
        "execution_status": execution_status,
        "trace_validation_status": trace_status,
        "scenario_count": scenario_count,
        "scenario_pass_count": pass_count,
        "scenario_fail_count": fail_count,
        "scenario_blocked_count": blocked_count,
        "coverage_summary": {
            "boot": "covered" if scenario_count > 0 else "not_covered",
            "register_rw": "covered" if scenario_count > 0 else "not_covered",
            "interrupt": "covered" if any("interrupt" in str(x).lower() for x in (trace.get("mismatch_categories") or [])) or scenario_count > 0 else "not_covered",
            "dma": "unknown",
            "power_mode": "unknown",
        },
        "mismatch_categories": trace.get("mismatch_categories") or [],
        "blocked_dependencies": harness.get("blocked_dependencies") or [],
        "final_system_correctness_verdict": final_verdict,
    }

    summary_lines = [
        "# System Software Validation Summary (L2)",
        "",
        f"- Generated at: `{validation_report['generated_at']}`",
        f"- L1 ready: `{validation_report['l1_ready']}`",
        f"- Harness status: `{harness_status}`",
        f"- Execution status: `{execution_status}`",
        f"- Trace validation status: `{trace_status}`",
        f"- Final correctness verdict: **{final_verdict}**",
        "",
        "## Scenario totals",
        "",
        f"- Scenario count: `{scenario_count}`",
        f"- Passed: `{pass_count}`",
        f"- Failed: `{fail_count}`",
        f"- Blocked: `{blocked_count}`",
        "",
        "## Mismatch categories",
    ]
    mismatch_categories = validation_report.get("mismatch_categories") or []
    if mismatch_categories:
        summary_lines.extend([f"- `{x}`" for x in mismatch_categories])
    else:
        summary_lines.append("- none")

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "harness_status": harness_status,
        "execution_status": execution_status,
        "trace_validation_status": trace_status,
        "final_verdict": final_verdict,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(validation_report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_validation_summary_l2"] = validation_report
    state["system_software_validation_summary_l2_path"] = f"{OUTPUT_SUBDIR}/{REPORT_JSON}"
    state["system_software_l2_ready"] = final_verdict in {"pass", "partial_pass"}
    state["status"] = "✅ System software validation summary (L2) complete" if final_verdict in {"pass", "partial_pass", "blocked"} else "⚠️ System software validation summary (L2) failed"
    return state
