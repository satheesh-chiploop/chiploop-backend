import json
import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


# ---------------------------------------------------------------------
# System Loop: Firmware Coverage Summary Agent
# - Consumes firmware co-sim execution summary and optional downstream
#   coverage/report artifacts
# - Avoids fake coverage when execution was blocked or not attempted
# - Produces dashboard-style JSON + Markdown summary for System_Firmware
# ---------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _pick_first_dict(state: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    for k in keys:
        v = state.get(k)
        if isinstance(v, dict):
            return v
    return {}


def _parse_embedded_coverage(state: Dict[str, Any]) -> Dict[str, Any]:
    cov = _pick_first_dict(
        state,
        [
            "embedded_coverage",
            "coverage_summary",
            "firmware_coverage",
            "embedded_coverage_summary",
        ],
    )

    # Normalize common field names if present
    return {
        "raw": cov,
        "functional_pct": (
            _safe_float(cov.get("functional_coverage_pct"))
            if isinstance(cov, dict)
            else None
        ),
        "rtl_pct": (
            _safe_float(cov.get("rtl_coverage_pct"))
            if isinstance(cov, dict)
            else None
        ),
        "assertion_pct": (
            _safe_float(cov.get("assertion_coverage_pct"))
            if isinstance(cov, dict)
            else None
        ),
        "test_count": (
            _safe_int(cov.get("test_count"), 0)
            if isinstance(cov, dict)
            else 0
        ),
    }


def _parse_validation_report(state: Dict[str, Any]) -> Dict[str, Any]:
    rep = _pick_first_dict(
        state,
        [
            "embedded_validation_report",
            "validation_report",
            "firmware_validation_report",
        ],
    )
    return rep if isinstance(rep, dict) else {}


def _coverage_allowed(execution: Dict[str, Any]) -> bool:
    if not isinstance(execution, dict):
        return False

    status = str(execution.get("overall_status") or "").strip().lower()
    results = execution.get("results") or {}
    attempted = bool(results.get("attempted"))
    executed_test_count = _safe_int(results.get("executed_test_count"), 0)

    # Only allow coverage numbers if we have evidence that execution actually ran
    return attempted or executed_test_count > 0 or status == "executed"


def _normalized_metrics(
    execution: Dict[str, Any],
    embedded_cov: Dict[str, Any],
) -> Dict[str, Any]:
    allow = _coverage_allowed(execution)

    if not allow:
        return {
            "functional_coverage_pct": None,
            "rtl_coverage_pct": None,
            "assertion_coverage_pct": None,
            "coverage_available": False,
            "coverage_reason": "Execution was not attempted or did not complete; coverage is unavailable.",
        }

    return {
        "functional_coverage_pct": embedded_cov.get("functional_pct"),
        "rtl_coverage_pct": embedded_cov.get("rtl_pct"),
        "assertion_coverage_pct": embedded_cov.get("assertion_pct"),
        "coverage_available": True,
        "coverage_reason": "Coverage values were permitted because execution artifacts indicate a real run.",
    }


def _aggregate_health(
    execution: Dict[str, Any],
    metrics: Dict[str, Any],
    validation_report: Dict[str, Any],
) -> Dict[str, Any]:
    overall_status = execution.get("overall_status") or "unknown"
    readiness = ((execution.get("readiness") or {}).get("status")) or "unknown"
    results = execution.get("results") or {}

    executed = _safe_int(results.get("executed_test_count"), 0)
    failed = _safe_int(results.get("failed_test_count"), 0)
    passed = _safe_int(results.get("passed_test_count"), 0)

    validation_status = str(validation_report.get("status") or "").strip() if isinstance(validation_report, dict) else ""

    if not metrics.get("coverage_available"):
        headline = "blocked"
    elif failed > 0:
        headline = "failed"
    elif executed > 0 and passed == executed:
        headline = "passed"
    else:
        headline = "partial"

    return {
        "headline_status": headline,
        "execution_overall_status": overall_status,
        "execution_readiness": readiness,
        "executed_test_count": executed,
        "passed_test_count": passed,
        "failed_test_count": failed,
        "validation_status": validation_status or None,
    }


def _notes(
    execution: Dict[str, Any],
    metrics: Dict[str, Any],
    embedded_cov: Dict[str, Any],
) -> List[str]:
    out = []

    if not metrics.get("coverage_available"):
        out.append("Coverage percentages are intentionally left unavailable because execution did not actually run.")
    else:
        out.append("Coverage metrics are allowed because execution artifacts indicate a real run.")

    if embedded_cov.get("functional_pct") is None:
        out.append("No firmware functional coverage percentage was detected from upstream embedded coverage artifacts.")

    if embedded_cov.get("rtl_pct") is None:
        out.append("No RTL/code coverage percentage was detected from upstream embedded coverage artifacts.")

    if embedded_cov.get("assertion_pct") is None:
        out.append("No assertion coverage percentage was detected from upstream embedded coverage artifacts.")

    exec_missing = ((execution.get("readiness") or {}).get("missing")) or []
    if exec_missing:
        out.append("Missing execution inputs recorded upstream: " + ", ".join(exec_missing))

    return out


def _markdown_report(summary: Dict[str, Any]) -> str:
    metrics = summary.get("coverage_metrics", {})
    health = summary.get("health", {})
    notes = summary.get("notes", [])
    execution = summary.get("execution", {})

    lines = []
    lines.append("# System Firmware Coverage Summary")
    lines.append("")
    lines.append(f"- Generated at: {summary.get('generated_at')}")
    lines.append(f"- Headline status: {health.get('headline_status')}")
    lines.append(f"- Execution status: {health.get('execution_overall_status')}")
    lines.append(f"- Execution readiness: {health.get('execution_readiness')}")
    lines.append("")

    lines.append("## Coverage Metrics")
    lines.append("")
    lines.append(
        f"- Functional coverage: `{metrics.get('functional_coverage_pct') if metrics.get('functional_coverage_pct') is not None else 'unavailable'}`"
    )
    lines.append(
        f"- RTL/code coverage: `{metrics.get('rtl_coverage_pct') if metrics.get('rtl_coverage_pct') is not None else 'unavailable'}`"
    )
    lines.append(
        f"- Assertion coverage: `{metrics.get('assertion_coverage_pct') if metrics.get('assertion_coverage_pct') is not None else 'unavailable'}`"
    )
    lines.append(f"- Coverage available: `{metrics.get('coverage_available')}`")
    lines.append("")

    lines.append("## Run Health")
    lines.append("")
    lines.append(f"- Executed tests: `{health.get('executed_test_count')}`")
    lines.append(f"- Passed tests: `{health.get('passed_test_count')}`")
    lines.append(f"- Failed tests: `{health.get('failed_test_count')}`")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for n in notes:
        lines.append(f"- {n}")
    lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    lines.append(metrics.get("coverage_reason") or "No conclusion available.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_agent(state: dict) -> dict:
    agent_name = "System Firmware Coverage Summary Agent"
    workflow_id = state.get("workflow_id")

    print("\n📊 Running System Firmware Coverage Summary Agent")

    execution = _pick_first_dict(
        state,
        [
            "system_firmware_execution",
            "system_sim_execution",
            "execution_summary",
        ],
    )
    dashboard_in = _pick_first_dict(
        state,
        [
            "system_firmware_dashboard",
            "system_sim_dashboard",
        ],
    )
    embedded_cov = _parse_embedded_coverage(state)
    validation_report = _parse_validation_report(state)

    metrics = _normalized_metrics(execution, embedded_cov)
    health = _aggregate_health(execution, metrics, validation_report)
    notes = _notes(execution, metrics, embedded_cov)

    summary = {
        "agent": agent_name,
        "generated_at": _now(),
        "coverage_metrics": metrics,
        "health": health,
        "execution": {
            "overall_status": execution.get("overall_status"),
            "readiness": execution.get("readiness"),
            "results": execution.get("results"),
        },
        "inputs_observed": {
            "execution_present": bool(execution),
            "dashboard_present": bool(dashboard_in),
            "embedded_coverage_present": bool(embedded_cov.get("raw")),
            "validation_report_present": bool(validation_report),
        },
        "notes": notes,
    }

    dashboard = {
        "title": "System Firmware Coverage Dashboard",
        "generated_at": summary["generated_at"],
        "headline_status": health["headline_status"],
        "execution_overall_status": health["execution_overall_status"],
        "coverage_available": metrics["coverage_available"],
        "functional_coverage_pct": metrics["functional_coverage_pct"],
        "rtl_coverage_pct": metrics["rtl_coverage_pct"],
        "assertion_coverage_pct": metrics["assertion_coverage_pct"],
        "executed_test_count": health["executed_test_count"],
        "passed_test_count": health["passed_test_count"],
        "failed_test_count": health["failed_test_count"],
        "summary_path": "system/firmware/coverage/system_firmware_coverage_summary.md",
    }

    md = _markdown_report(summary)

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/coverage",
        "system_firmware_coverage_summary.json",
        json.dumps(summary, indent=2),
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/coverage",
        "system_firmware_coverage_summary.md",
        md,
    )

    state["system_firmware_coverage_summary"] = summary
    state["system_firmware_coverage_dashboard"] = dashboard
    state["system_firmware_coverage_summary_path"] = "system/firmware/coverage/system_firmware_coverage_summary.json"
    state["system_firmware_coverage_summary_md_path"] = "system/firmware/coverage/system_firmware_coverage_summary.md"
    state["status"] = "✅ System firmware coverage summary generated"
    return state