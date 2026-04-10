import datetime
import json
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software CoSim Trace Validation Agent"
OUTPUT_SUBDIR = "system/software_validation/cosim/trace"
REPORT_JSON = "system_cosim_trace_validation_report.json"
SUMMARY_MD = "system_cosim_trace_validation_summary.md"
DEBUG_JSON = "system_cosim_trace_validation_debug.json"


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


def _listify(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _dictify(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _missing_expected_items(expected: List[Any], observed: List[Any]) -> List[Any]:
    observed_set = set(str(x) for x in observed)
    return [x for x in expected if str(x) not in observed_set]


def _validate_registers(expected: Dict[str, Any], observed: Dict[str, Any]) -> List[Dict[str, Any]]:
    mismatches: List[Dict[str, Any]] = []
    for reg_name, exp_value in expected.items():
        if reg_name not in observed:
            mismatches.append({
                "type": "register_missing",
                "register": reg_name,
                "expected": exp_value,
                "observed": None,
            })
        elif observed.get(reg_name) != exp_value:
            mismatches.append({
                "type": "register_mismatch",
                "register": reg_name,
                "expected": exp_value,
                "observed": observed.get(reg_name),
            })
    return mismatches


def _scenario_enabled_map(state: Dict[str, Any]) -> Dict[str, bool]:
    harness = state.get("system_software_cosim_harness_manifest") or {}
    scenarios = harness.get("scenarios") or []
    out: Dict[str, bool] = {}
    for item in scenarios:
        if isinstance(item, dict):
            sid = str(item.get("scenario_id") or item.get("id") or "").strip()
            if sid:
                out[sid] = bool(item.get("enabled", True))
    return out


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    execution = state.get("system_software_cosim_execution_report") or {}
    scenario_results = execution.get("scenario_results") or []

    if not scenario_results:
        report = {
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "trace_validation_status": "blocked",
            "message": "No scenario execution results available.",
            "scenario_validations": [],
            "mismatch_categories": [],
        }
        _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
        _record_text(workflow_id, SUMMARY_MD, "# System Software CoSim Trace Validation Summary\n\n- Status: **blocked**\n- Message: `No scenario execution results available.`\n")
        _record_text(workflow_id, DEBUG_JSON, json.dumps({
            "agent": AGENT_NAME,
            "generated_at": _now(),
            "scenario_count": 0,
        }, indent=2))
        state["system_software_cosim_trace_validation_report"] = report
        state["system_software_cosim_trace_validation_status"] = "blocked"
        state["status"] = "⚠️ System software co-sim trace validation blocked"
        return state

    scenario_validations: List[Dict[str, Any]] = []
    mismatch_categories: List[str] = []

    scenario_enabled = _scenario_enabled_map(state)

    for item in scenario_results:
        scenario_id = str(item.get("scenario_id") or "").strip()
        enabled = scenario_enabled.get(scenario_id, True)
        expected = item.get("expected_behavior") or {}
        observed = item.get("observed_behavior") or {}

        expected_events = _listify(expected.get("expected_events"))
        observed_events = _listify(observed.get("observed_events"))
        missing_events = _missing_expected_items(expected_events, observed_events)

        expected_interrupts = _listify(expected.get("expected_interrupts"))
        observed_interrupts = _listify(observed.get("observed_interrupts"))
        missing_interrupts = _missing_expected_items(expected_interrupts, observed_interrupts)

        expected_signals = _listify(expected.get("expected_signals"))
        observed_signals = _listify(observed.get("observed_signals"))
        missing_signals = _missing_expected_items(expected_signals, observed_signals)

        expected_registers = _dictify(expected.get("expected_registers"))
        observed_registers = _dictify(observed.get("observed_registers"))
        register_mismatches = _validate_registers(expected_registers, observed_registers)

        mismatches: List[Dict[str, Any]] = []
        for ev in missing_events:
            mismatches.append({"type": "event_missing", "expected": ev})
        for irq in missing_interrupts:
            mismatches.append({"type": "interrupt_missing", "expected": irq})
        for sig in missing_signals:
            mismatches.append({"type": "signal_missing", "expected": sig})
        mismatches.extend(register_mismatches)

        if not enabled:
            mismatches.append({
                "type": "disabled_scenario_executed",
                "scenario_id": scenario_id,
            })

        if item.get("execution_status") != "pass":
            mismatches.append({
                "type": "execution_failed",
                "returncode": item.get("returncode"),
            })

        for mm in mismatches:
            mtype = str(mm.get("type") or "").strip()
            if mtype and mtype not in mismatch_categories:
                mismatch_categories.append(mtype)

        validation_status = "pass" if not mismatches else "fail"
        scenario_validations.append({
            "scenario_id": scenario_id,
            "scenario_type": item.get("scenario_type") or "",
            "trace_validation_status": validation_status,
            "expected_behavior": expected,
            "observed_behavior": observed,
            "mismatches": mismatches,
        })

    pass_count = sum(1 for x in scenario_validations if x.get("trace_validation_status") == "pass")
    fail_count = sum(1 for x in scenario_validations if x.get("trace_validation_status") == "fail")
    overall_status = "pass" if fail_count == 0 else ("partial_pass" if pass_count > 0 else "fail")

    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "trace_validation_status": overall_status,
        "scenario_count": len(scenario_validations),
        "scenario_pass_count": pass_count,
        "scenario_fail_count": fail_count,
        "mismatch_categories": mismatch_categories,
        "scenario_validations": scenario_validations,
    }

    summary_lines = [
        "# System Software CoSim Trace Validation Summary",
        "",
        f"- Trace validation status: **{overall_status}**",
        f"- Scenario count: `{len(scenario_validations)}`",
        f"- Passed: `{pass_count}`",
        f"- Failed: `{fail_count}`",
        "",
        "## Mismatch categories",
    ]
    if mismatch_categories:
        summary_lines.extend([f"- `{x}`" for x in mismatch_categories])
    else:
        summary_lines.append("- none")

    summary_lines.extend(["", "## Scenario validations"])
    for item in scenario_validations:
        summary_lines.append(
            f"- `{item['scenario_id']}` → status=`{item['trace_validation_status']}` mismatches=`{len(item['mismatches'])}`"
        )

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "scenario_count": len(scenario_validations),
        "mismatch_categories": mismatch_categories,
    }

    _record_text(workflow_id, REPORT_JSON, json.dumps(report, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_cosim_trace_validation_report"] = report
    state["system_software_cosim_trace_validation_status"] = overall_status
    state["status"] = "✅ System software co-sim trace validation complete" if overall_status in {"pass", "partial_pass"} else "⚠️ System software co-sim trace validation failed"
    return state
