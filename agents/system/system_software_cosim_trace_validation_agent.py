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

def _normalize_token(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")

def _semantic_aliases(state: Dict[str, Any]) -> Dict[str, List[str]]:
    harness = state.get("system_software_cosim_harness_manifest") or {}
    semantic_assets = harness.get("semantic_assets") or {}
    integration = (
        state.get("system_integration_intent")
        or semantic_assets.get("integration_intent_json")
        or {}
    )

    aliases = {
        "reset_released": ["rst_n=1", "reset_n=1", "rst_n", "reset_n", "deassert_reset"],
    }

    event_paths = integration.get("event_paths") or []
    if isinstance(event_paths, list):
        for item in event_paths:
            if not isinstance(item, dict):
                continue
            rtl_event = str(item.get("rtl_event") or "").strip()
            firmware_handler = str(item.get("firmware_handler") or "").strip()
            software_effect = str(item.get("software_effect") or "").strip()

            if rtl_event:
                aliases.setdefault(rtl_event, [])
                aliases[rtl_event].extend([
                    rtl_event,
                    f"interrupt_{rtl_event}",
                    f"{rtl_event}_irq",
                    f"irq_{rtl_event}",
                ])

            if firmware_handler:
                aliases.setdefault(firmware_handler, [])
                aliases[firmware_handler].append(firmware_handler)

            if software_effect:
                aliases.setdefault(software_effect, [])
                aliases[software_effect].append(software_effect)

    return aliases


def _matches_expected_token(expected: Any, observed: List[Any], aliases: Dict[str, List[str]]) -> bool:
    exp = _normalize_token(expected)
    observed_norm = [_normalize_token(x) for x in observed]
    if exp in observed_norm:
        return True

    for alias in aliases.get(exp, []):
        if _normalize_token(alias) in observed_norm:
            return True

    observed_blob = "\n".join(observed_norm)
    if exp in observed_blob:
        return True
    for alias in aliases.get(exp, []):
        if _normalize_token(alias) in observed_blob:
            return True
    return False


def _missing_expected_items(expected: List[Any], observed: List[Any], aliases: Dict[str, List[str]]) -> List[Any]:
    return [x for x in expected if not _matches_expected_token(x, observed, aliases)]

def _normalize_register_map(observed: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in observed.items():
        out[str(k).strip().upper()] = v
    return out


def _normalize_scalar(value: Any) -> str:
    return str(value).strip().lower()


def _validate_registers(expected: Dict[str, Any], observed: Dict[str, Any]) -> List[Dict[str, Any]]:
    mismatches: List[Dict[str, Any]] = []
    observed_norm = _normalize_register_map(observed)

    for reg_name, exp_value in expected.items():
        reg_key = str(reg_name).strip().upper()
        if reg_key not in observed_norm:
            mismatches.append({
                "type": "register_missing",
                "register": reg_name,
                "expected": exp_value,
                "observed": None,
            })
        elif _normalize_scalar(observed_norm.get(reg_key)) != _normalize_scalar(exp_value):
            mismatches.append({
                "type": "register_mismatch",
                "register": reg_name,
                "expected": exp_value,
                "observed": observed_norm.get(reg_key),
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



        aliases = _semantic_aliases(state)

        expected_events = _listify(expected.get("expected_events"))
        observed_events = _listify(observed.get("observed_events"))
        missing_events = _missing_expected_items(expected_events, observed_events, aliases)

        expected_interrupts = _listify(expected.get("expected_interrupts"))
        observed_interrupts = _listify(observed.get("observed_interrupts"))
        missing_interrupts = _missing_expected_items(expected_interrupts, observed_interrupts, aliases)

        expected_signals = _listify(expected.get("expected_signals"))
        observed_signals = _listify(observed.get("observed_signals"))
        missing_signals = _missing_expected_items(expected_signals, observed_signals, aliases)

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

        # If scenario is disabled → do NOT validate it as pass/fail
        if not enabled:
            scenario_validations.append({
                "scenario_id": scenario_id,
                "scenario_type": item.get("scenario_type") or "",
                "trace_validation_status": "not_applicable",
                "expected_behavior": expected,
                "observed_behavior": observed,
                "mismatches": [],
                "note": "Scenario disabled by contract; not validated.",
            })
            continue

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
    na_count = sum(1 for x in scenario_validations if x.get("trace_validation_status") == "not_applicable")
    applicable_count = pass_count + fail_count

    if applicable_count == 0:
        overall_status = "blocked"
    elif fail_count == 0:
        overall_status = "pass"
    elif pass_count > 0:
        overall_status = "partial_pass"
    else:
        overall_status = "fail"



    report = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "trace_validation_status": overall_status,
        "scenario_count": len(scenario_validations),
        "scenario_pass_count": pass_count,
        "scenario_fail_count": fail_count,
        "scenario_not_applicable_count": na_count,
        "scenario_applicable_count": applicable_count,
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
