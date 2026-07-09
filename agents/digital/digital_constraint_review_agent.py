import json
import re
from pathlib import Path
from typing import Any, Dict, List

from agents.digital.digital_review_utils import collect_rtl_sources, collect_sdc_text, markdown_table, rtl_clock_signals
from tooling.runner import run_command, run_tool_diagnostics
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital Constraint Review Agent"


def _finding(severity: str, category: str, message: str, recommendation: str) -> Dict[str, str]:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "recommendation": recommendation,
    }


def _commands(sdc_text: str, command: str) -> List[str]:
    pattern = re.compile(rf"^\s*{re.escape(command)}\b(.+)$", re.IGNORECASE | re.MULTILINE)
    return [match.group(0).strip() for match in pattern.finditer(sdc_text)]


def _clock_names(create_clock_lines: List[str]) -> List[str]:
    names = set()
    for line in create_clock_lines:
        name_match = re.search(r"-name\s+([A-Za-z_][A-Za-z0-9_$./-]*)", line)
        if name_match:
            names.add(name_match.group(1))
        object_match = re.search(r"\[get_ports\s+\{?([A-Za-z_][A-Za-z0-9_$./-]*)", line)
        if object_match:
            names.add(object_match.group(1))
    return sorted(names)


def _tail(text: str, max_chars: int = 6000) -> str:
    return (text or "")[-max_chars:]


def _tool_record(result: Any) -> Dict[str, Any]:
    return {
        "tool": getattr(result, "tool", ""),
        "status": getattr(result, "status", "unknown"),
        "available": getattr(result, "status", "") != "tool_unavailable",
        "returncode": getattr(result, "returncode", None),
        "executable": getattr(result, "executable", None),
        "command": getattr(result, "command", []),
        "stdout_tail": _tail(getattr(result, "stdout", "")),
        "stderr_tail": _tail(getattr(result, "stderr", "")),
        "error": getattr(result, "error", None),
    }


def _run_sdc_tool_checks(state: Dict[str, Any], out_dir: Path, sdc_text: str) -> Dict[str, Any]:
    diagnostics = run_tool_diagnostics(state, tools=["sta", "yosys"], timeout_sec=10)
    if not sdc_text.strip():
        return {"diagnostics": diagnostics, "executions": {}, "summary": {"passed": 0, "failed": 0, "unavailable": 0}}

    sdc_path = out_dir / "constraint_review_input.sdc"
    tcl_path = out_dir / "constraint_review_opensta.tcl"
    sdc_path.write_text(sdc_text, encoding="utf-8")
    tcl_path.write_text(
        "\n".join([
            f"read_sdc {sdc_path}",
            "report_clocks",
            "report_clock_properties",
            "exit",
        ]),
        encoding="utf-8",
    )
    executions = {
        "opensta_sdc_parse": _tool_record(run_command(
            state,
            "digital_constraint_review.opensta_sdc_parse",
            ["sta", "-exit", str(tcl_path)],
            cwd=str(out_dir),
            timeout_sec=60,
        ))
    }
    passed = sum(1 for item in executions.values() if item.get("status") == "success")
    failed = sum(1 for item in executions.values() if item.get("status") == "failed")
    unavailable = sum(1 for item in executions.values() if item.get("status") == "tool_unavailable")
    return {
        "diagnostics": diagnostics,
        "executions": executions,
        "summary": {"passed": passed, "failed": failed, "unavailable": unavailable, "executed": len(executions)},
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or state.get("artifact_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "constraint_review"
    out_dir.mkdir(parents=True, exist_ok=True)

    sdc_text = collect_sdc_text(state)
    tool_results = _run_sdc_tool_checks(state, out_dir, sdc_text)
    rtl_sources = collect_rtl_sources(state)
    rtl_clocks = rtl_clock_signals(rtl_sources)

    create_clocks = _commands(sdc_text, "create_clock")
    generated_clocks = _commands(sdc_text, "create_generated_clock")
    false_paths = _commands(sdc_text, "set_false_path")
    multicycle_paths = _commands(sdc_text, "set_multicycle_path")
    input_delays = _commands(sdc_text, "set_input_delay")
    output_delays = _commands(sdc_text, "set_output_delay")
    clock_groups = _commands(sdc_text, "set_clock_groups")
    uncertainty = _commands(sdc_text, "set_clock_uncertainty")

    clock_names = _clock_names(create_clocks + generated_clocks)
    findings: List[Dict[str, str]] = []
    if not sdc_text.strip():
        findings.append(_finding(
            "high",
            "missing_constraints",
            "No SDC constraint text was provided or discovered.",
            "Provide create_clock plus IO delay constraints before synthesis or STA.",
        ))
    if rtl_clocks and not create_clocks:
        findings.append(_finding(
            "high",
            "missing_primary_clocks",
            f"RTL uses clocked logic on {', '.join(rtl_clocks[:8])}, but no create_clock command is present.",
            "Add create_clock for each primary clock port used by sequential logic.",
        ))
    missing = [clk for clk in rtl_clocks if clk not in clock_names]
    if create_clocks and missing:
        findings.append(_finding(
            "medium",
            "clock_coverage",
            f"RTL clock-like signals not clearly covered by SDC: {', '.join(missing[:8])}.",
            "Check whether these are real clocks, generated clocks, or data enables.",
        ))
    if create_clocks and not input_delays and not output_delays:
        findings.append(_finding(
            "medium",
            "io_timing",
            "No input or output delay constraints were detected.",
            "Add IO delay assumptions for block-level signoff, or document why all IO is internal.",
        ))
    if create_clocks and not uncertainty:
        findings.append(_finding(
            "low",
            "margining",
            "No clock uncertainty constraint was detected.",
            "Add uncertainty or margin settings matching the target methodology.",
        ))
    if false_paths and len(false_paths) > max(3, len(create_clocks) * 3):
        findings.append(_finding(
            "medium",
            "exception_review",
            "False path count is high relative to clock count.",
            "Review exceptions for over-broad masking before relying on timing closure.",
        ))
    if len(generated_clocks) == 0 and any(re.search(r"\b(clk|clock).*(div|gate|mux)", src["content"], re.IGNORECASE) for src in rtl_sources):
        findings.append(_finding(
            "medium",
            "generated_clock_risk",
            "RTL appears to contain divided/gated/muxed clock logic, but no generated clocks were detected.",
            "Model generated clocks or convert clock gating/division to methodology-approved cells.",
        ))

    summary = {
        "status": "blocked_no_constraints" if not sdc_text.strip() else "review_completed",
        "rtl_clock_count": len(rtl_clocks),
        "create_clock_count": len(create_clocks),
        "generated_clock_count": len(generated_clocks),
        "input_delay_count": len(input_delays),
        "output_delay_count": len(output_delays),
        "false_path_count": len(false_paths),
        "multicycle_path_count": len(multicycle_paths),
        "clock_group_count": len(clock_groups),
        "finding_count": len(findings),
        "target_frequency_mhz": state.get("target_frequency_mhz"),
        "tool_pass_count": tool_results["summary"]["passed"],
        "tool_fail_count": tool_results["summary"]["failed"],
        "tool_unavailable_count": tool_results["summary"]["unavailable"],
    }
    report = {
        "type": "digital_constraint_review",
        "agent": AGENT_NAME,
        "summary": summary,
        "rtl_clocks": rtl_clocks,
        "sdc_clock_names": clock_names,
        "tools": tool_results,
        "findings": findings,
    }

    md = "\n".join([
        "# Digital Constraint Review",
        "",
        f"- Status: `{summary['status']}`",
        f"- RTL clocks detected: {summary['rtl_clock_count']}",
        f"- SDC primary clocks: {summary['create_clock_count']}",
        f"- Generated clocks: {summary['generated_clock_count']}",
        f"- Findings: {summary['finding_count']}",
        f"- Tool checks: {summary['tool_pass_count']} passed, {summary['tool_fail_count']} failed, {summary['tool_unavailable_count']} unavailable",
        "",
        "## Tool Checks",
        "",
        markdown_table(
            ["Tool", "Status", "Return code", "Executable"],
            [[name, item.get("status"), item.get("returncode"), item.get("executable") or ""] for name, item in tool_results["executions"].items()],
        ) if tool_results["executions"] else "No tool checks were executed.",
        "",
        "## Constraint Coverage",
        "",
        markdown_table(
            ["Metric", "Count"],
            [
                ["create_clock", summary["create_clock_count"]],
                ["create_generated_clock", summary["generated_clock_count"]],
                ["set_input_delay", summary["input_delay_count"]],
                ["set_output_delay", summary["output_delay_count"]],
                ["set_false_path", summary["false_path_count"]],
                ["set_multicycle_path", summary["multicycle_path_count"]],
                ["set_clock_groups", summary["clock_group_count"]],
            ],
        ),
        "",
        "## Findings",
        "",
        markdown_table(["Severity", "Category", "Finding", "Recommendation"], [[f["severity"], f["category"], f["message"], f["recommendation"]] for f in findings]) if findings else "No constraint issues detected by heuristic review.",
        "",
    ])

    report_text = json.dumps(report, indent=2)
    (out_dir / "constraint_review.json").write_text(report_text, encoding="utf-8")
    (out_dir / "constraint_review.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "constraint_review", "constraint_review.json", report_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "constraint_review", "constraint_review.md", md)

    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    digital["constraint_review"] = report
    state["digital"] = digital
    state["constraint_review"] = report
    return state
