import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.digital.digital_review_utils import collect_timing_reports, markdown_table
from tooling.runner import run_tool_diagnostics
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital Timing Debug Agent"


def _float_after(patterns: List[str], text: str) -> Optional[float]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1))
        except Exception:
            continue
    return None


def _extract_metrics(content: str) -> Dict[str, Optional[float]]:
    compact = content.replace("\n", " ")
    return {
        "wns": _float_after([r"\bWNS\b[^-+0-9]*([-+]?\d+(?:\.\d+)?)", r"wns\"?\s*:\s*([-+]?\d+(?:\.\d+)?)"], compact),
        "tns": _float_after([r"\bTNS\b[^-+0-9]*([-+]?\d+(?:\.\d+)?)", r"tns\"?\s*:\s*([-+]?\d+(?:\.\d+)?)"], compact),
        "hold_wns": _float_after([r"\bhold[^A-Za-z0-9]+WNS\b[^-+0-9]*([-+]?\d+(?:\.\d+)?)", r"hold_wns\"?\s*:\s*([-+]?\d+(?:\.\d+)?)"], compact),
        "setup_wns": _float_after([r"\bsetup[^A-Za-z0-9]+WNS\b[^-+0-9]*([-+]?\d+(?:\.\d+)?)", r"setup_wns\"?\s*:\s*([-+]?\d+(?:\.\d+)?)"], compact),
    }


def _classify(content: str, metrics: Dict[str, Optional[float]]) -> List[Dict[str, str]]:
    lowered = content.lower()
    findings: List[Dict[str, str]] = []
    wns = metrics.get("wns")
    setup_wns = metrics.get("setup_wns")
    hold_wns = metrics.get("hold_wns")
    if wns is not None and wns < 0:
        findings.append({
            "severity": "high",
            "category": "negative_slack",
            "message": f"Negative WNS detected: {wns}.",
            "recommendation": "Inspect top violating paths, check target frequency, and rerun with sizing/buffering or pipeline guidance.",
        })
    if setup_wns is not None and setup_wns < 0:
        findings.append({
            "severity": "high",
            "category": "setup_violation",
            "message": f"Setup WNS is negative: {setup_wns}.",
            "recommendation": "Review long combinational paths, high fanout drivers, and missing generated clock constraints.",
        })
    if hold_wns is not None and hold_wns < 0:
        findings.append({
            "severity": "high",
            "category": "hold_violation",
            "message": f"Hold WNS is negative: {hold_wns}.",
            "recommendation": "Check clock uncertainty/skew assumptions and enable hold buffering in implementation.",
        })
    if "unconstrained" in lowered:
        findings.append({
            "severity": "high",
            "category": "unconstrained_paths",
            "message": "Timing report mentions unconstrained paths.",
            "recommendation": "Fix missing clocks, generated clocks, IO delays, or false/multicycle exception coverage before trusting STA.",
        })
    if "high fanout" in lowered or "fanout" in lowered:
        findings.append({
            "severity": "medium",
            "category": "fanout",
            "message": "Report references fanout concerns.",
            "recommendation": "Add buffering, register duplication, or synthesis/placement fanout constraints for the named nets.",
        })
    if "clock not found" in lowered or "no clock" in lowered or "missing clock" in lowered:
        findings.append({
            "severity": "high",
            "category": "missing_clock",
            "message": "Report suggests missing clock definitions.",
            "recommendation": "Run Constraint Review and add create_clock/create_generated_clock coverage.",
        })
    return findings


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or state.get("artifact_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "timing_debug"
    out_dir.mkdir(parents=True, exist_ok=True)

    reports = collect_timing_reports(state)
    diagnostics = run_tool_diagnostics(state, tools=["sta", "openroad"], timeout_sec=10)
    analyzed: List[Dict[str, Any]] = []
    all_findings: List[Dict[str, str]] = []
    for report in reports:
        metrics = _extract_metrics(report["content"])
        findings = _classify(report["content"], metrics)
        analyzed.append({
            "path": report["path"],
            "metrics": metrics,
            "finding_count": len(findings),
            "findings": findings,
        })
        all_findings.extend(findings)

    worst_wns = None
    for item in analyzed:
        for key in ("wns", "setup_wns", "hold_wns"):
            value = item["metrics"].get(key)
            if value is None:
                continue
            worst_wns = value if worst_wns is None else min(worst_wns, value)

    summary = {
        "status": "blocked_no_timing_report" if not reports else "debug_completed",
        "timing_report_count": len(reports),
        "finding_count": len(all_findings),
        "worst_slack_observed": worst_wns,
        "target_frequency_mhz": state.get("target_frequency_mhz"),
        "stage": state.get("stage") or state.get("timing_stage") or "auto",
        "opensta_available": bool((diagnostics.get("sta") or {}).get("available")),
        "openroad_available": bool((diagnostics.get("openroad") or {}).get("available")),
    }
    report_obj = {
        "type": "digital_timing_debug",
        "agent": AGENT_NAME,
        "summary": summary,
        "tools": {"diagnostics": diagnostics},
        "reports": analyzed,
        "recommended_next_steps": [
            "Run Constraint Review if unconstrained or missing clock evidence is present.",
            "Use synthesis closure for pre-place setup violations.",
            "Use signoff closure for post-route setup/hold violations.",
        ],
    }

    metric_rows = [
        [item["path"], item["metrics"].get("wns"), item["metrics"].get("setup_wns"), item["metrics"].get("hold_wns"), item["finding_count"]]
        for item in analyzed
    ]
    finding_rows = [
        [finding["severity"], finding["category"], finding["message"], finding["recommendation"]]
        for finding in all_findings
    ]
    md = "\n".join([
        "# Digital Timing Debug",
        "",
        f"- Status: `{summary['status']}`",
        f"- Reports analyzed: {summary['timing_report_count']}",
        f"- Worst slack observed: `{summary['worst_slack_observed']}`",
        f"- Findings: {summary['finding_count']}",
        f"- OpenSTA available: `{summary['opensta_available']}`",
        f"- OpenROAD available: `{summary['openroad_available']}`",
        "",
        "## Metrics",
        "",
        markdown_table(["Report", "WNS", "Setup WNS", "Hold WNS", "Findings"], metric_rows) if metric_rows else "No timing report text was available.",
        "",
        "## Findings",
        "",
        markdown_table(["Severity", "Category", "Finding", "Recommendation"], finding_rows) if finding_rows else "No timing issues detected by heuristic review.",
        "",
    ])

    report_text = json.dumps(report_obj, indent=2)
    (out_dir / "timing_debug.json").write_text(report_text, encoding="utf-8")
    (out_dir / "timing_debug.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "timing_debug", "timing_debug.json", report_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "timing_debug", "timing_debug.md", md)

    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    digital["timing_debug"] = report_obj
    state["digital"] = digital
    state["timing_debug"] = report_obj
    return state
