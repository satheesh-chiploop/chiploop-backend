import json
import os
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital DQA Summary Agent"


def _read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                value = json.load(fh)
                return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def _first_existing(paths: list[str]) -> str:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return ""


def _status_from_report(report: Dict[str, Any], default: str = "unavailable") -> str:
    status = report.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    findings = report.get("findings")
    if isinstance(findings, list) and findings:
        severities = {str(item.get("severity", "")).lower() for item in findings if isinstance(item, dict)}
        return "warning" if "warning" in severities else "issues"
    return default if not report else "pass"


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "digital", "dqa")
    os.makedirs(out_dir, exist_ok=True)

    lint_path = _first_existing([
        os.path.join(workflow_dir, "digital", "rtl_lint_report.json"),
        state.get("rtl_lint_report_path") if isinstance(state.get("rtl_lint_report_path"), str) else "",
    ])
    cdc_path = _first_existing([
        os.path.join(workflow_dir, "digital", "cdc_findings.json"),
        state.get("cdc_report_path") if isinstance(state.get("cdc_report_path"), str) else "",
    ])
    reset_path = _first_existing([
        os.path.join(workflow_dir, "digital", "reset_integrity_findings.json"),
        state.get("reset_integrity_report_path") if isinstance(state.get("reset_integrity_report_path"), str) else "",
    ])
    synth_path = _first_existing([
        os.path.join(workflow_dir, "signoff", "synthesis_readiness_findings.json"),
    ])

    lint = _read_json(lint_path)
    cdc = _read_json(cdc_path)
    reset = _read_json(reset_path)
    synth = _read_json(synth_path)

    blocking = []
    lint_status = _status_from_report(lint)
    if lint_status == "fail":
        blocking.append("rtl_lint_failed")
    if synth.get("yosys", {}).get("errors"):
        blocking.append("yosys_synthesis_errors")

    warning_count = 0
    for report in (cdc, reset):
        findings = report.get("findings")
        if isinstance(findings, list):
            warning_count += len(findings)
    synth_warnings = synth.get("yosys", {}).get("warnings")
    if isinstance(synth_warnings, list):
        warning_count += len(synth_warnings)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "failed" if blocking else "warning" if warning_count else "pass",
        "blocking_issues": blocking,
        "warning_count": warning_count,
        "rtl_file_count": (
            lint.get("rtl_file_count")
            or cdc.get("inputs", {}).get("rtl_file_count")
            or reset.get("rtl_file_count")
            or synth.get("rtl_file_count")
        ),
        "reports": {
            "rtl_lint": lint_path or None,
            "cdc": cdc_path or None,
            "reset_integrity": reset_path or None,
            "synthesis_readiness": synth_path or None,
        },
        "stages": {
            "rtl_lint": {"status": lint_status, "heuristic_issue_count": (lint.get("summary") or {}).get("heuristic_issue_count")},
            "cdc": {"status": _status_from_report(cdc), "findings": len(cdc.get("findings") or []) if isinstance(cdc.get("findings"), list) else None},
            "reset_integrity": {"status": _status_from_report(reset), "findings": len(reset.get("findings") or []) if isinstance(reset.get("findings"), list) else None},
            "synthesis_readiness": {
                "status": "failed" if synth.get("yosys", {}).get("errors") else "pass" if synth else "unavailable",
                "score": synth.get("score"),
                "yosys_warning_count": len(synth_warnings) if isinstance(synth_warnings, list) else None,
            },
        },
    }

    md = [
        "# Digital DQA Summary",
        f"- workflow_id: `{workflow_id}`",
        f"- status: `{summary['status']}`",
        f"- rtl files: `{summary['rtl_file_count']}`",
        f"- warning count: `{warning_count}`",
        "",
        "## Stage Status",
    ]
    for name, details in summary["stages"].items():
        md.append(f"- {name}: `{details.get('status')}`")
    if blocking:
        md.append("")
        md.append("## Blocking Issues")
        md.extend([f"- {issue}" for issue in blocking])
    text_json = json.dumps(summary, indent=2)
    text_md = "\n".join(md) + "\n"

    json_path = os.path.join(out_dir, "dqa_summary.json")
    md_path = os.path.join(out_dir, "dqa_summary.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(text_json)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(text_md)

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/dqa", "dqa_summary.json", text_json)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/dqa", "dqa_summary.md", text_md)
    state.setdefault("digital", {})["dqa_summary"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
