import json
import os
from typing import Any, Dict

from ._embedded_common import ensure_workflow_dir, write_artifact

AGENT_NAME = "Embedded Validation Report Agent"
PHASE = "report"
OUTPUT_PATH = "firmware/validate/validation_report.md"


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _safe_load_json(path: str) -> Dict[str, Any]:
    text = _safe_read(path)
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)
    workflow_dir = state.get("workflow_dir") or ""

    cosim_obj = state.get("system_firmware_execution") or _safe_load_json(
        os.path.join(workflow_dir, "system/firmware/cosim/system_firmware_execution.json")
    )
    coverage_obj = state.get("system_firmware_coverage_summary") or _safe_load_json(
        os.path.join(workflow_dir, "system/firmware/coverage/system_firmware_coverage_summary.json")
    )

    execution_status = (cosim_obj.get("overall_status") or "unavailable") if isinstance(cosim_obj, dict) else "unavailable"
    readiness_status = (((cosim_obj.get("readiness") or {}).get("status")) if isinstance(cosim_obj, dict) else None) or "unavailable"
    results = (cosim_obj.get("results") or {}) if isinstance(cosim_obj, dict) else {}
    attempted = results.get("attempted", "unavailable")
    executed = results.get("executed_test_count", "unavailable")
    passed = results.get("passed_test_count", "unavailable")
    failed = results.get("failed_test_count", "unavailable")

    cov_metrics = (coverage_obj.get("coverage_metrics") or {}) if isinstance(coverage_obj, dict) else {}
    functional_cov = cov_metrics.get("functional_coverage_pct", "unavailable")
    rtl_cov = cov_metrics.get("rtl_coverage_pct", "unavailable")
    assertion_cov = cov_metrics.get("assertion_coverage_pct", "unavailable")
    coverage_available = cov_metrics.get("coverage_available", "unavailable")

    lines = [
        "# Validation Report",
        "",
        f"- Co-simulation overall status: {execution_status}",
        f"- Readiness status: {readiness_status}",
        f"- Execution attempted: {attempted}",
        f"- Executed tests: {executed}",
        f"- Passed tests: {passed}",
        f"- Failed tests: {failed}",
        "",
        "## Coverage",
        f"- Functional coverage: {functional_cov}",
        f"- RTL coverage: {rtl_cov}",
        f"- Assertion coverage: {assertion_cov}",
        f"- Coverage available: {coverage_available}",
        "",
        "## Notes",
        "- This report is generated directly from downstream execution and coverage artifacts.",
        "- Missing values are reported as unavailable rather than inferred.",
    ]

    if execution_status == "ready_with_placeholder_elf":
        lines.append("- Execution readiness was achieved using a placeholder ELF, so runtime claims remain limited.")

    out = "\n".join(lines) + "\n"
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    report_obj = {
        "status": execution_status,
        "readiness": readiness_status,
        "coverage_available": coverage_available,
        "validation_report_path": OUTPUT_PATH,
    }
    state["embedded_validation_report"] = report_obj
    state["validation_report_path"] = OUTPUT_PATH
    state["firmware_validation_report_path"] = OUTPUT_PATH
    state["validation_report_generated"] = True
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    return state
