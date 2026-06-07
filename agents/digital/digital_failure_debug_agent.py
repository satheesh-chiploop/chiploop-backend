import json
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
    if any(token in text for token in ("modulenotfound", "importerror", "no module named", "make:")):
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
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    options = _debug_options(state)
    triage = state.get("failure_triage") if isinstance(state.get("failure_triage"), dict) else {}
    failures = [item for item in (triage.get("failures") or []) if isinstance(item, dict)]
    debug_items: List[Dict[str, Any]] = []
    if options["enabled"]:
        debug_items = [_classify_failure(failure, options) for failure in failures]

    report = {
        "type": "failure_debug",
        "enabled": options["enabled"],
        "options": options,
        "failure_count": len(failures),
        "debugged_failure_count": len(debug_items),
        "items": debug_items,
        "summary": (
            "disabled" if not options["enabled"]
            else "no_failures" if not failures
            else "debug_recommendations_generated"
        ),
    }
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
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_debug.json", txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "failure_debug.md", md)
    state["failure_debug"] = report
    return state
