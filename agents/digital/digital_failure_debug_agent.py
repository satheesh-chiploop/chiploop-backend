import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Failure Debug Agent"


def _debug_options(state: Dict[str, Any]) -> Dict[str, Any]:
    opts = state.get("failure_debug_options") if isinstance(state.get("failure_debug_options"), dict) else {}
    return {
        "enabled": bool(opts.get("enabled") or state.get("enable_failure_debug")),
        "log_only_first": opts.get("log_only_first", True) is not False,
        "generate_vcd_if_inconclusive": opts.get("generate_vcd_if_inconclusive", True) is not False,
        "auto_apply_testbench_fixes": bool(opts.get("auto_apply_testbench_fixes")),
        "auto_apply_rtl_fixes": bool(opts.get("auto_apply_rtl_fixes")),
        "rerun_failing_tests": opts.get("rerun_failing_tests", True) is not False,
    }


def _joined(failure: Dict[str, Any]) -> str:
    stdout = failure.get("stdout_tail") if isinstance(failure.get("stdout_tail"), list) else []
    stderr = failure.get("stderr_tail") if isinstance(failure.get("stderr_tail"), list) else []
    return "\n".join(str(line) for line in [*stdout, *stderr]).lower()


def _classify_failure(failure: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    text = _joined(failure)
    triage_class = str(failure.get("classification") or "unknown")
    testcase = failure.get("testcase")
    seed = failure.get("seed")

    root_cause = "inconclusive"
    confidence = "low"
    fix_domain = "manual_debug"
    analysis = "Logs do not contain enough expected/actual or assertion context to isolate a fix."
    if triage_class == "verification_collateral_compile_or_elaboration_failure":
        root_cause = "verification_collateral_compile_or_elaboration_failure"
        confidence = "high"
        fix_domain = "verification_collateral_or_toolchain"
        analysis = "Compilation/elaboration failed before RTL behavior was exercised; RTL repair is not justified."
    elif triage_class == "simulation_timeout":
        root_cause = "simulation_timeout"
        confidence = "high"
        fix_domain = "testbench_clock_reset_or_rtl_deadlock"
        analysis = "The bounded simulation timed out; isolate termination, clock/reset, and deadlock behavior before repair."
    elif any(token in text for token in ("modulenotfound", "importerror", "no module named", "make:")):
        root_cause = "testbench_or_environment_issue"
        confidence = "medium"
        fix_domain = "testbench_or_environment"
        analysis = "Failure text points to generated testbench, Python import, Makefile, or tool setup rather than RTL behavior."
    elif any(token in text for token in ("scoreboard", "mismatch", "expected", "actual", "observed")):
        root_cause = "rtl_or_reference_mismatch"
        confidence = "medium"
        fix_domain = "rtl_or_scoreboard"
        analysis = "Failure includes mismatch language; compare expected and observed transaction values before patching RTL."
    elif "assert" in text or "sva" in text:
        root_cause = "assertion_failure"
        confidence = "medium"
        fix_domain = "rtl_or_assertion"
        analysis = "Assertion failure is visible in logs; waveform is recommended if the firing cycle or antecedent is not printed."
    elif triage_class in {"environment_or_testbench_failure"}:
        root_cause = "testbench_or_environment_issue"
        confidence = "medium"
        fix_domain = "testbench_or_environment"
        analysis = "Failure triage classified this as environment/testbench before RTL debug."

    needs_vcd = root_cause == "inconclusive" or root_cause in {"assertion_failure", "rtl_or_reference_mismatch"}
    vcd_recommended = bool(options["generate_vcd_if_inconclusive"] and needs_vcd)
    patch_allowed = (
        (fix_domain == "testbench_or_environment" and options["auto_apply_testbench_fixes"])
        or (fix_domain in {"rtl_or_scoreboard", "rtl_or_assertion"} and options["auto_apply_rtl_fixes"])
    )

    assertion_failures = [item for item in failure.get("assertion_failures") or [] if isinstance(item, dict)]
    return {
        "testcase": testcase,
        "seed": seed,
        "triage_classification": triage_class,
        "root_cause_classification": root_cause,
        "confidence": confidence,
        "analysis": analysis,
        "fix_domain": fix_domain,
        "auto_patch_allowed": patch_allowed,
        "patch_policy": "proposal_only" if not patch_allowed else "auto_apply_enabled",
        "recommended_next_step": (
            "rerun_failed_test_with_vcd" if vcd_recommended
            else "rerun_failed_test_log_only" if options["rerun_failing_tests"]
            else "review_failure_report"
        ),
        "targeted_rerun": {
            "testcase": testcase,
            "seed": seed,
            "enable_waveform": vcd_recommended,
            "scope": "single_testcase_seed",
        },
        "assertion_failures": assertion_failures,
    }


def _rtl_repair_request(debug_items: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    failures = [
        failure
        for item in debug_items
        for failure in item.get("assertion_failures") or []
        if isinstance(failure, dict)
    ]
    normalized = sorted({
        f"{item.get('requirement_id')}|{item.get('checker_id')}|{item.get('owner_module')}"
        for item in failures
    })
    fingerprint = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest() if normalized else None
    history = state.get("behavioral_repair_history") if isinstance(state.get("behavioral_repair_history"), list) else []
    repeated = sum(1 for item in history if isinstance(item, dict) and item.get("fingerprint") == fingerprint)
    max_attempts = max(1, int(state.get("behavioral_repair_max_attempts") or 3))
    return {
        "type": "behavioral_rtl_repair_request",
        "required": bool(failures),
        "fingerprint": fingerprint,
        "attempt": repeated + 1 if failures else 0,
        "max_attempts": max_attempts,
        "status": (
            "not_required" if not failures
            else "blocked_nonconvergent" if repeated >= max_attempts
            else "ready_for_targeted_rtl_repair"
        ),
        "rules": {
            "preserve_interfaces": True,
            "incremental_module_only": True,
            "rerun_failed_test_first": True,
            "run_full_regression_after_focused_pass": True,
        },
        "failures": failures,
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    options = _debug_options(state)
    triage = state.get("failure_triage") if isinstance(state.get("failure_triage"), dict) else {}
    failures = [item for item in (triage.get("failures") or []) if isinstance(item, dict)]
    # Requirement-linked assertion failures are safe to process automatically:
    # they already carry an executable checker ID and exact RTL requirement.
    # The UI debug toggle still controls heuristic debugging of unstructured
    # failures, but must not silently disable the behavioral repair strategy.
    structured_failures = [failure for failure in failures if failure.get("assertion_failures")]
    selected_failures = failures if options["enabled"] else structured_failures
    debug_items: List[Dict[str, Any]] = [
        _classify_failure(failure, options) for failure in selected_failures
    ]

    report = {
        "type": "failure_debug",
        "enabled": options["enabled"],
        "options": options,
        "failure_count": len(failures),
        "debugged_failure_count": len(debug_items),
        "items": debug_items,
        "summary": (
            "structured_assertion_failures_auto_debugged" if structured_failures and not options["enabled"]
            else "disabled" if not options["enabled"]
            else "no_failures" if not failures
            else "debug_recommendations_generated"
        ),
    }
    repair_request = _rtl_repair_request(debug_items, state)
    report["rtl_repair_request"] = repair_request
    txt = json.dumps(report, indent=2)
    md_lines = [
        "# Failure Debug",
        "",
        f"- Enabled: {options['enabled']}",
        f"- Failing testcase/seed pairs: {len(failures)}",
        f"- Debug recommendations: {len(debug_items)}",
        "",
    ]
    for item in debug_items:
        md_lines.extend([
            f"## {item.get('testcase')} seed {item.get('seed')}",
            "",
            f"- Root cause class: `{item['root_cause_classification']}`",
            f"- Confidence: `{item['confidence']}`",
            f"- Patch policy: `{item['patch_policy']}`",
            f"- Recommended next step: `{item['recommended_next_step']}`",
            f"- Analysis: {item['analysis']}",
            "",
        ])
    md = "\n".join(md_lines)
    (out_dir / "failure_debug.json").write_text(txt, encoding="utf-8")
    (out_dir / "failure_debug.md").write_text(md, encoding="utf-8")
    repair_txt = json.dumps(repair_request, indent=2)
    (out_dir / "rtl_repair_request.json").write_text(repair_txt, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_debug.json", txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_debug.md", md)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "rtl_repair_request.json", repair_txt)
    state["failure_debug"] = report
    state["rtl_repair_request"] = repair_request
    if repair_request.get("required"):
        history = state.get("behavioral_repair_history") if isinstance(state.get("behavioral_repair_history"), list) else []
        state["behavioral_repair_history"] = [*history, {
            "fingerprint": repair_request.get("fingerprint"),
            "attempt": repair_request.get("attempt"),
        }]
    return state
