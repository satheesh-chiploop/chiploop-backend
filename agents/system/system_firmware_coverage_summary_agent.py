import datetime
import json
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Firmware Coverage Summary Agent"
OUTPUT_SUBDIR = "system/firmware/coverage"
SUMMARY_JSON = "system_firmware_coverage_summary.json"
SUMMARY_MD = "system_firmware_coverage_summary.md"
DASHBOARD_JSON = "system_firmware_coverage_dashboard.json"


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


def _pick_first_dict_from_containers(containers: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Any]:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _all_state_containers(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    containers: List[Dict[str, Any]] = [state]
    for key in ("system", "embedded", "firmware", "firmware_build"):
        block = state.get(key)
        if isinstance(block, dict):
            containers.append(block)
    return containers


def _parse_embedded_coverage(state: Dict[str, Any]) -> Dict[str, Any]:
    containers = _all_state_containers(state)
    cov = _pick_first_dict_from_containers(
        containers,
        [
            "embedded_coverage",
            "coverage_summary",
            "firmware_coverage",
            "embedded_coverage_summary",
            "coverage_metrics",
        ],
    )
    return {
        "raw": cov,
        "functional_pct": _safe_float(cov.get("functional_coverage_pct")) if cov else None,
        "rtl_pct": _safe_float(cov.get("rtl_coverage_pct")) if cov else None,
        "assertion_pct": _safe_float(cov.get("assertion_coverage_pct")) if cov else None,
        "test_count": _safe_int(cov.get("test_count"), 0) if cov else 0,
    }


def _parse_validation_report(state: Dict[str, Any]) -> Dict[str, Any]:
    return _pick_first_dict_from_containers(
        _all_state_containers(state),
        [
            "embedded_validation_report",
            "validation_report",
            "firmware_validation_report",
        ],
    )


def _coverage_allowed(execution: Dict[str, Any]) -> bool:
    if not isinstance(execution, dict):
        return False
    status = str(execution.get("overall_status") or "").strip().lower()
    results = execution.get("results") or {}
    attempted = bool(results.get("attempted"))
    executed_test_count = _safe_int(results.get("executed_test_count"), 0)
    return attempted or executed_test_count > 0 or status in {"executed", "simulation_passed", "simulation_failed"}


def _normalized_metrics(execution: Dict[str, Any], embedded_cov: Dict[str, Any]) -> Dict[str, Any]:
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
        "coverage_available": any(
            x is not None
            for x in (
                embedded_cov.get("functional_pct"),
                embedded_cov.get("rtl_pct"),
                embedded_cov.get("assertion_pct"),
            )
        ),
        "coverage_reason": "Coverage values were permitted because execution artifacts indicate a real run.",
    }


def _aggregate_health(execution: Dict[str, Any], metrics: Dict[str, Any], validation_report: Dict[str, Any]) -> Dict[str, Any]:
    overall_status = execution.get("overall_status") or "unknown"
    readiness = ((execution.get("readiness") or {}).get("status")) or "unknown"
    results = execution.get("results") or {}
    executed = _safe_int(results.get("executed_test_count"), 0)
    failed = _safe_int(results.get("failed_test_count"), 0)
    passed = _safe_int(results.get("passed_test_count"), 0)
    validation_status = str(validation_report.get("status") or "").strip() if isinstance(validation_report, dict) else ""

    if not metrics.get("coverage_available"):
        headline = "blocked" if readiness != "ready" else "partial"
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


def _notes(execution: Dict[str, Any], metrics: Dict[str, Any], embedded_cov: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    out.append(
        "Coverage metrics are allowed because execution artifacts indicate a real run."
        if metrics.get("coverage_available")
        else "Coverage percentages are intentionally left unavailable because execution did not actually run or no coverage numbers were provided."
    )
    if embedded_cov.get("functional_pct") is None:
        out.append("No firmware functional coverage percentage was detected from upstream coverage artifacts.")
    if embedded_cov.get("rtl_pct") is None:
        out.append("No RTL/code coverage percentage was detected from upstream coverage artifacts.")
    if embedded_cov.get("assertion_pct") is None:
        out.append("No assertion coverage percentage was detected from upstream coverage artifacts.")
    exec_missing = ((execution.get("readiness") or {}).get("missing")) or []
    if exec_missing:
        out.append("Missing execution inputs recorded upstream: " + ", ".join(exec_missing))
    return out


def _markdown_report(summary: Dict[str, Any]) -> str:
    metrics = summary.get("coverage_metrics", {})
    health = summary.get("health", {})
    notes = summary.get("notes", [])
    lines = [
        "# System Firmware Coverage Summary",
        "",
        f"- Generated at: {summary.get('generated_at')}",
        f"- Headline status: {health.get('headline_status')}",
        f"- Execution status: {health.get('execution_overall_status')}",
        f"- Execution readiness: {health.get('execution_readiness')}",
        "",
        "## Coverage Metrics",
        "",
        f"- Functional coverage: `{metrics.get('functional_coverage_pct') if metrics.get('functional_coverage_pct') is not None else 'unavailable'}`",
        f"- RTL/code coverage: `{metrics.get('rtl_coverage_pct') if metrics.get('rtl_coverage_pct') is not None else 'unavailable'}`",
        f"- Assertion coverage: `{metrics.get('assertion_coverage_pct') if metrics.get('assertion_coverage_pct') is not None else 'unavailable'}`",
        f"- Coverage available: `{metrics.get('coverage_available')}`",
        "",
        "## Run Health",
        "",
        f"- Executed tests: `{health.get('executed_test_count')}`",
        f"- Passed tests: `{health.get('passed_test_count')}`",
        f"- Failed tests: `{health.get('failed_test_count')}`",
        "",
        "## Notes",
        "",
    ]
    lines.extend([f"- {n}" for n in notes])
    lines.extend(["", "## Conclusion", "", metrics.get("coverage_reason") or "No conclusion available.", ""])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    print("\n📊 Running System Firmware Coverage Summary Agent")

    containers = _all_state_containers(state)
    execution = _pick_first_dict_from_containers(
        containers,
        ["system_firmware_execution", "system_sim_execution", "execution_summary"],
    )
    dashboard_in = _pick_first_dict_from_containers(
        containers,
        ["system_firmware_dashboard", "system_sim_dashboard"],
    )
    embedded_cov = _parse_embedded_coverage(state)
    validation_report = _parse_validation_report(state)

    metrics = _normalized_metrics(execution, embedded_cov)
    health = _aggregate_health(execution, metrics, validation_report)
    notes = _notes(execution, metrics, embedded_cov)

    summary = {
        "agent": AGENT_NAME,
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
        "summary_path": f"{OUTPUT_SUBDIR}/{SUMMARY_MD}",
    }

    md = _markdown_report(summary)

    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, SUMMARY_JSON, json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, SUMMARY_MD, md)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, DASHBOARD_JSON, json.dumps(dashboard, indent=2))

    state["system_firmware_coverage_summary"] = summary
    state["system_firmware_coverage_dashboard"] = dashboard
    state["system_firmware_coverage_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_JSON}"
    state["system_firmware_coverage_summary_md_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"
    state["system_firmware_coverage_dashboard_path"] = f"{OUTPUT_SUBDIR}/{DASHBOARD_JSON}"
    state["status"] = "✅ System firmware coverage summary generated"
    return state
