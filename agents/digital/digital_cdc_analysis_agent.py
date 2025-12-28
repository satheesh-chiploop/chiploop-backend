import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

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


def _find_clock_reset_arch(workflow_dir: str, state: dict) -> Optional[dict]:
    # prefer explicit state path
    p = state.get("clock_reset_arch_path")
    if isinstance(p, str) and p.endswith(".json") and os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    # scan workflow dir
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn == "clock_reset_arch_intent.json":
                with open(os.path.join(root, fn), "r", encoding="utf-8") as f:
                    return json.load(f)
    return None


def _infer_clock_from_always(text: str) -> Optional[str]:
    # very simple: always @(posedge clk or negedge rst_n)
    m = re.search(r"always\s*@\s*\(\s*posedge\s+([A-Za-z_]\w*)", text)
    if m:
        return m.group(1)
    return None


def run_agent(state: dict) -> dict:
    agent_name = "CDC Analysis Agent"
    print("\n🔀 Running CDC Analysis Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    log_path = os.path.join("artifact", "digital_cdc_analysis_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("CDC Analysis Agent Log\n")

    arch = _find_clock_reset_arch(workflow_dir, state)
    domains = []
    clk_to_domain = {}
    if arch:
        domains = [d.get("name") for d in arch.get("domains", []) if isinstance(d, dict)]
        for c in arch.get("clocks", []):
            if isinstance(c, dict) and c.get("name") and c.get("domain"):
                clk_to_domain[c["name"]] = c["domain"]
        _log(log_path, f"Loaded clock/reset intent with {len(domains)} domains and {len(clk_to_domain)} clocks.")
    else:
        _log(log_path, "No clock/reset intent found. Using heuristic-only mode.")

    rtl_files = _collect_rtl_files(workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files.")

    always_blocks = []
    # Heuristic: find clock per file and list candidate sequential regs
    for fp in rtl_files:
        txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
        clk = _infer_clock_from_always(txt)
        dom = clk_to_domain.get(clk) if clk else None
        always_blocks.append({"file": fp, "clock": clk, "domain": dom})

    # Heuristic crossing guess:
    # If multiple domains exist and some modules have different clocks, report "potential CDC boundary".
    used_domains = sorted({b["domain"] for b in always_blocks if b.get("domain")})
    used_clks = sorted({b["clock"] for b in always_blocks if b.get("clock")})

    findings = []
    if len(used_domains) > 1:
        findings.append({
            "type": "potential_multi_domain_design",
            "severity": "warning",
            "msg": f"Multiple clock domains detected: {used_domains}. CDC verification required."
        })
    elif len(used_clks) > 1 and not used_domains:
        findings.append({
            "type": "potential_multi_clock_design",
            "severity": "warning",
            "msg": f"Multiple clocks inferred in RTL: {used_clks}. Consider providing clock intent JSON."
        })

    recommendations = [
        "Provide clock_reset_arch_intent.json for higher-fidelity CDC intent (domains, allowed crossings).",
        "For single-bit control crossings: use 2-flop synchronizers.",
        "For multi-bit data: use async FIFOs or validated handshake schemes.",
        "Run a real CDC tool in enterprise flow (Questa CDC / SpyGlass CDC / VC CDC) when available."
    ]

    report = {
        "type": "cdc_analysis_report",
        "version": "1.0",
        "inputs": {
            "clock_reset_intent_present": bool(arch),
            "rtl_file_count": len(rtl_files),
        },
        "observations": {
            "inferred_clocks": used_clks,
            "inferred_domains": used_domains,
            "per_file": always_blocks,
        },
        "findings": findings,
        "recommendations": recommendations,
        "note": "This agent provides intent-level CDC screening. It is not a replacement for signoff CDC tools."
    }

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "cdc_analysis_report.json", json.dumps(report, indent=2))
    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_cdc_analysis_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["cdc_report_path"] = os.path.join(workflow_dir, "digital", "cdc_analysis_report.json")
    return state
