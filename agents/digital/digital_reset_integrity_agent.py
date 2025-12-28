import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any

from utils.artifact_utils import save_text_artifact_and_record


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _collect_rtl_files(workflow_dir: str) -> List[str]:
    rtl = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.endswith((".v", ".sv")):
                rtl.append(os.path.join(root, fn))
    return sorted(rtl)


def run_agent(state: dict) -> dict:
    agent_name = "Reset Integrity Agent"
    print("\n🧯 Running Reset Integrity Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    log_path = os.path.join("artifact", "digital_reset_integrity_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Reset Integrity Agent Log\n")

    rtl_files = _collect_rtl_files(workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files.")

    patterns = {
        "async_reset_sens": re.compile(r"always\s*@\s*\([^)]*(negedge|posedge)\s+([A-Za-z_]\w*)", re.IGNORECASE),
        "reset_if": re.compile(r"if\s*\(\s*!?([A-Za-z_]\w*)\s*\)", re.IGNORECASE),
    }

    reset_names = set()
    async_blocks = []
    reset_usage = []

    for fp in rtl_files:
        txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
        # find resets in sensitivity list
        for m in patterns["async_reset_sens"].finditer(txt):
            edge, sig = m.group(1), m.group(2)
            if "rst" in sig.lower() or "reset" in sig.lower():
                reset_names.add(sig)
                async_blocks.append({"file": fp, "reset": sig, "edge": edge})

        # find reset in if statements (heuristic)
        for m in patterns["reset_if"].finditer(txt):
            sig = m.group(1)
            if "rst" in sig.lower() or "reset" in sig.lower():
                reset_names.add(sig)
                reset_usage.append({"file": fp, "reset": sig, "context": "if_condition"})

    findings = []
    if len(reset_names) == 0:
        findings.append({
            "type": "no_reset_detected",
            "severity": "warning",
            "msg": "No reset signal detected by heuristic. Consider providing explicit reset intent in spec."
        })

    # Mixed async resets heuristic
    reset_edges = {}
    for b in async_blocks:
        reset_edges.setdefault(b["reset"], set()).add(b["edge"].lower())
    mixed = [r for r, edges in reset_edges.items() if len(edges) > 1]
    if mixed:
        findings.append({
            "type": "mixed_reset_edges",
            "severity": "warning",
            "msg": f"Reset(s) appear with mixed edges in sensitivity list: {mixed}. Standardize reset polarity/edge."
        })

    recommendations = [
        "Prefer async-assert / sync-deassert reset strategy in multi-clock designs.",
        "Ensure reset deassertion is synchronized per clock domain.",
        "Avoid mixing async and sync reset styles without clear intent.",
        "Add reset-specific assertions: no X after reset release; stable reset sequencing."
    ]

    report = {
        "type": "reset_integrity_report",
        "version": "1.0",
        "rtl_file_count": len(rtl_files),
        "detected_reset_signals": sorted(reset_names),
        "async_reset_blocks": async_blocks,
        "reset_usage_locations": reset_usage[:200],
        "findings": findings,
        "recommendations": recommendations,
        "note": "Heuristic scan only; use signoff reset/CDC checks in enterprise flows when available."
    }

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "reset_integrity_report.json", json.dumps(report, indent=2))
    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_reset_integrity_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["reset_integrity_report_path"] = os.path.join(workflow_dir, "digital", "reset_integrity_report.json")
    return state
