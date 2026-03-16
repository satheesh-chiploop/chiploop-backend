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
   
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}
   
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

COSIM EXECUTION SUMMARY:
{cosim_summary if cosim_summary else "(not available)"}

COVERAGE SUMMARY:
{coverage_summary if coverage_summary else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate validation report from cosim logs and coverage.

RULES:
- Prefer COSIM + COVERAGE artifacts when available.
- Fall back to USER SPEC if artifacts are missing.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/validate/validation_report.md
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If execution summary indicates blocked or missing inputs, report that explicitly.
- Do NOT invent successful execution.
- Do NOT invent coverage percentages.
- If data is unavailable, say "unavailable" or "blocked" instead of assuming values.
"""
    out = deterministic_report

    if not cosim_summary and not coverage_summary:
        out = """# Validation Report

- Co-simulation summary: unavailable
- Coverage summary: unavailable
- Validation status: blocked

## Notes
- Required downstream execution artifacts were not found.
"""
    elif not out.strip():
        out = f"""# Validation Report

- Co-simulation summary: {"available" if cosim_summary else "unavailable"}
- Coverage summary: {"available" if coverage_summary else "unavailable"}
- Validation status: generated

## Notes
- Report was generated from available downstream artifacts.
- Missing data is reported as unavailable rather than assumed.
"""

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
