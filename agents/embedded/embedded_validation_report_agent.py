import json
import os
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Validation Report Agent"
PHASE = "report"
OUTPUT_PATH = "firmware/validate/validation_report.md"
def _safe_read(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""




    cosim_summary = _safe_read(os.path.join(workflow_dir, "system/firmware/cosim/system_firmware_execution.json"))
    coverage_summary = (
       _safe_read(os.path.join(workflow_dir, "system/firmware/coverage/system_firmware_coverage_summary.json"))
       or _safe_read(os.path.join(workflow_dir, "coverage/coverage_summary.json"))
    )

    try:
        cosim_obj = json.loads(cosim_summary) if cosim_summary else {}
    except Exception:
        cosim_obj = {}

    try:
        coverage_obj = json.loads(coverage_summary) if coverage_summary else {}
    except Exception:
        coverage_obj = {}

    execution_status = (cosim_obj.get("overall_status") or "unavailable") if isinstance(cosim_obj, dict) else "unavailable"
    readiness_status = (((cosim_obj.get("readiness") or {}).get("status")) if isinstance(cosim_obj, dict) else None) or "unavailable"
    attempted = (((cosim_obj.get("results") or {}).get("attempted")) if isinstance(cosim_obj, dict) else None)
    executed = (((cosim_obj.get("results") or {}).get("executed_test_count")) if isinstance(cosim_obj, dict) else None)
    passed = (((cosim_obj.get("results") or {}).get("passed_test_count")) if isinstance(cosim_obj, dict) else None)
    failed = (((cosim_obj.get("results") or {}).get("failed_test_count")) if isinstance(cosim_obj, dict) else None)

    cov_metrics = (coverage_obj.get("coverage_metrics") or {}) if isinstance(coverage_obj, dict) else {}
    functional_cov = cov_metrics.get("functional_coverage_pct")
    rtl_cov = cov_metrics.get("rtl_coverage_pct")
    assertion_cov = cov_metrics.get("assertion_coverage_pct")
    coverage_available = cov_metrics.get("coverage_available")

    deterministic_report = f"""# Validation Report

- Co-simulation overall status: {execution_status}
- Readiness status: {readiness_status}
- Execution attempted: {attempted if attempted is not None else "unavailable"}
- Executed tests: {executed if executed is not None else "unavailable"}
- Passed tests: {passed if passed is not None else "unavailable"}
- Failed tests: {failed if failed is not None else "unavailable"}

## Coverage
- Functional coverage: {functional_cov if functional_cov is not None else "unavailable"}
- RTL coverage: {rtl_cov if rtl_cov is not None else "unavailable"}
- Assertion coverage: {assertion_cov if assertion_cov is not None else "unavailable"}
- Coverage available: {coverage_available if coverage_available is not None else "unavailable"}

## Notes
- This report is generated directly from downstream execution and coverage artifacts.
- Missing values are reported as unavailable rather than inferred.
"""
   
    out = deterministic_report

    if not cosim_summary and not coverage_summary:
        out = """# Validation Report

- Co-simulation overall status: unavailable
- Readiness status: unavailable
- Execution attempted: unavailable
- Executed tests: unavailable
- Passed tests: unavailable
- Failed tests: unavailable

## Coverage
- Functional coverage: unavailable
- RTL coverage: unavailable
- Assertion coverage: unavailable
- Coverage available: unavailable

## Notes
- Required downstream execution artifacts were not found.
"""

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    state["validation_report_path"] = OUTPUT_PATH
    state["firmware_validation_report_path"] = OUTPUT_PATH
    state["validation_report_generated"] = True

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
