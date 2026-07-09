import json
import re
from pathlib import Path
from typing import Any, Dict, List

from agents.digital.digital_review_utils import collect_rtl_sources, markdown_table
from tooling.runner import run_command, run_tool_diagnostics
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital RTL Review Agent"


def _issue(severity: str, category: str, message: str, evidence: str, recommendation: str) -> Dict[str, str]:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _review_source(path: str, content: str) -> Dict[str, Any]:
    modules = re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", content)
    always_blocks = re.findall(r"\balways(?:_ff|_comb|_latch)?\b", content)
    assign_count = len(re.findall(r"\bassign\b", content))
    lines = content.splitlines()
    issues: List[Dict[str, str]] = []

    if not modules:
        issues.append(_issue(
            "high",
            "parseability",
            "No Verilog/SystemVerilog module declaration was detected.",
            path,
            "Confirm the file is RTL source and not a package-only or partial snippet.",
        ))
    if re.search(r"always\s*@\s*\*", content) and "always_comb" not in content:
        issues.append(_issue(
            "medium",
            "coding_style",
            "Combinational logic uses always @* instead of always_comb.",
            path,
            "Use always_comb where the target flow supports SystemVerilog to improve intent checking.",
        ))
    if re.search(r"always(?:_ff)?\s*@\s*\([^)]*(posedge|negedge)", content) and re.search(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_$\[\]\s]*\s=", content):
        issues.append(_issue(
            "medium",
            "sequential_assignments",
            "Blocking assignment syntax appears near sequential logic.",
            path,
            "Review clocked blocks and prefer nonblocking assignments for flop updates.",
        ))
    if re.search(r"\bcasex\b|\bcasez\b", content):
        issues.append(_issue(
            "medium",
            "x_handling",
            "casex/casez usage can hide X/Z behavior.",
            path,
            "Use unique/priority case with explicit defaults when possible.",
        ))
    if re.search(r"\bTODO\b|\bFIXME\b|\bsynthesis\s+translate_off\b", content, re.IGNORECASE):
        issues.append(_issue(
            "low",
            "handoff_readiness",
            "TODO/FIXME or synthesis translate_off content is present.",
            path,
            "Resolve intentional placeholders before product handoff.",
        ))
    if re.search(r"\bcase\s*\([^)]+\)(?:(?!endcase).)*$", content, re.IGNORECASE | re.DOTALL) and "default" not in content:
        issues.append(_issue(
            "medium",
            "latch_risk",
            "A case statement may be missing a default branch.",
            path,
            "Check for full assignment coverage to avoid latch inference.",
        ))

    return {
        "path": path,
        "line_count": len(lines),
        "module_count": len(modules),
        "modules": modules[:16],
        "always_block_count": len(always_blocks),
        "continuous_assign_count": assign_count,
        "issues": issues,
    }


def _safe_source_name(index: int, path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix not in {".sv", ".v", ".svh", ".vh"}:
        suffix = ".sv"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(path).stem).strip("._") or f"source_{index}"
    return f"{index:03d}_{stem}{suffix}"


def _materialize_sources(out_dir: Path, sources: List[Dict[str, str]]) -> List[str]:
    source_dir = out_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    paths: List[str] = []
    for idx, src in enumerate(sources):
        path = source_dir / _safe_source_name(idx, src.get("path", "source.sv"))
        path.write_text(src.get("content", ""), encoding="utf-8", errors="replace")
        paths.append(str(path.resolve()))
    return paths


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


def _run_tool_checks(state: Dict[str, Any], out_dir: Path, rtl_files: List[str]) -> Dict[str, Any]:
    diagnostics = run_tool_diagnostics(
        state,
        tools=["verible_verilog_lint", "slang", "sv2v", "verilator", "yosys"],
        timeout_sec=10,
    )
    if not rtl_files:
        return {"diagnostics": diagnostics, "executions": {}, "summary": {"passed": 0, "failed": 0, "unavailable": 0}}

    executions: Dict[str, Any] = {}
    verilog_files = [path for path in rtl_files if Path(path).suffix.lower() in {".sv", ".v"}]
    if verilog_files:
        executions["verible_verilog_lint"] = _tool_record(run_command(
            state,
            "digital_rtl_review.verible_lint",
            ["verible-verilog-lint", "--parse_fatal", *verilog_files],
            cwd=str(out_dir),
            timeout_sec=60,
        ))
        executions["slang"] = _tool_record(run_command(
            state,
            "digital_rtl_review.slang_parse",
            ["slang", "--color-diagnostics=false", *verilog_files],
            cwd=str(out_dir),
            timeout_sec=60,
        ))
        executions["sv2v"] = _tool_record(run_command(
            state,
            "digital_rtl_review.sv2v_convert",
            ["sv2v", *verilog_files],
            cwd=str(out_dir),
            timeout_sec=60,
        ))
        executions["verilator"] = _tool_record(run_command(
            state,
            "digital_rtl_review.verilator_lint",
            ["verilator", "--lint-only", "--sv", *verilog_files],
            cwd=str(out_dir),
            timeout_sec=90,
        ))
        yosys_script = out_dir / "rtl_review_yosys.ys"
        read_lines = "\n".join(f"read_verilog -sv {json.dumps(path)}" for path in verilog_files)
        yosys_script.write_text(
            "\n".join([
                read_lines,
                "hierarchy -check -auto-top",
                "proc",
                "check",
                "stat",
            ]),
            encoding="utf-8",
        )
        executions["yosys"] = _tool_record(run_command(
            state,
            "digital_rtl_review.yosys_check",
            ["yosys", "-q", "-s", str(yosys_script)],
            cwd=str(out_dir),
            timeout_sec=120,
        ))

    passed = sum(1 for item in executions.values() if item.get("status") == "success")
    failed = sum(1 for item in executions.values() if item.get("status") == "failed")
    unavailable = sum(1 for item in executions.values() if item.get("status") == "tool_unavailable")
    return {
        "diagnostics": diagnostics,
        "executions": executions,
        "summary": {
            "passed": passed,
            "failed": failed,
            "unavailable": unavailable,
            "executed": len(executions),
        },
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or state.get("artifact_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "rtl_review"
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = collect_rtl_sources(state)
    rtl_files = _materialize_sources(out_dir, sources)
    tool_results = _run_tool_checks(state, out_dir, rtl_files)
    reviews = [_review_source(src["path"], src["content"]) for src in sources]
    issue_count = sum(len(item["issues"]) for item in reviews)
    high_count = sum(1 for item in reviews for issue in item["issues"] if issue["severity"] == "high")
    medium_count = sum(1 for item in reviews for issue in item["issues"] if issue["severity"] == "medium")

    summary = {
        "status": "blocked_no_rtl" if not sources else "review_completed",
        "rtl_file_count": len(sources),
        "module_count": sum(item["module_count"] for item in reviews),
        "issue_count": issue_count,
        "high_issue_count": high_count,
        "medium_issue_count": medium_count,
        "review_depth": state.get("review_depth") or "standard",
        "tool_pass_count": tool_results["summary"]["passed"],
        "tool_fail_count": tool_results["summary"]["failed"],
        "tool_unavailable_count": tool_results["summary"]["unavailable"],
    }
    report = {
        "type": "digital_rtl_review",
        "agent": AGENT_NAME,
        "summary": summary,
        "tools": tool_results,
        "files": reviews,
    }

    rows = []
    for item in reviews:
        rows.append([
            item["path"],
            item["module_count"],
            item["always_block_count"],
            item["continuous_assign_count"],
            len(item["issues"]),
        ])
    issue_rows = []
    for item in reviews:
        for issue in item["issues"]:
            issue_rows.append([issue["severity"], issue["category"], item["path"], issue["message"], issue["recommendation"]])

    md = "\n".join([
        "# Digital RTL Review",
        "",
        f"- Status: `{summary['status']}`",
        f"- RTL files reviewed: {summary['rtl_file_count']}",
        f"- Modules detected: {summary['module_count']}",
        f"- Issues: {summary['issue_count']} ({summary['high_issue_count']} high, {summary['medium_issue_count']} medium)",
        f"- Tool checks: {summary['tool_pass_count']} passed, {summary['tool_fail_count']} failed, {summary['tool_unavailable_count']} unavailable",
        "",
        "## Tool Checks",
        "",
        markdown_table(
            ["Tool", "Status", "Return code", "Executable"],
            [[name, item.get("status"), item.get("returncode"), item.get("executable") or ""] for name, item in tool_results["executions"].items()],
        ) if tool_results["executions"] else "No tool checks were executed.",
        "",
        "## File Summary",
        "",
        markdown_table(["File", "Modules", "Always", "Assigns", "Issues"], rows) if rows else "No RTL source was available.",
        "",
        "## Findings",
        "",
        markdown_table(["Severity", "Category", "File", "Finding", "Recommendation"], issue_rows) if issue_rows else "No heuristic findings detected.",
        "",
    ])

    report_text = json.dumps(report, indent=2)
    (out_dir / "rtl_review.json").write_text(report_text, encoding="utf-8")
    (out_dir / "rtl_review.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "rtl_review", "rtl_review.json", report_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "rtl_review", "rtl_review.md", md)

    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    digital["rtl_review"] = report
    state["digital"] = digital
    state["rtl_review"] = report
    return state
