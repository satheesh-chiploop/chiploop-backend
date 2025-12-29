"""
ChipLoop Open-Source Signoff / Handoff Agents

Design goals:
- deterministic outputs
- derive intent from prior artifacts (`digital_spec_json`, RTL outputs, architecture) rather than hardcoding
- best-effort integration with open-source tools (Yosys/OpenROAD/Verilator/etc.)
- always produce: (1) human report (MD) and (2) machine findings (JSON)
"""

import os
import re
import json
import shutil
import hashlib
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _run(cmd: List[str], cwd: Optional[str]=None, timeout: int=300) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}
    except Exception as e:
        return {"cmd": cmd, "returncode": -1, "stdout": "", "stderr": str(e)}

def _record(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None

def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")
    out: List[str] = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                out.append(os.path.join(root, fn))
    out.sort()
    return out

def _pick_top(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    if state_top:
        return state_top
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()
    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return "top"

# ----------------------------
# Synthesis Readiness Agent (OSS: Yosys)
# ----------------------------

UNSYNTH_PATTERNS = [
    (r"\binitial\b", "Use of initial blocks may be non-synthesizable (ASIC) or tool-dependent (FPGA)."),
    (r"#\s*\d+", "Delay control (#N) is not synthesizable."),
    (r"\bforce\b|\brelease\b", "force/release are not synthesizable."),
    (r"\b\$display\b|\b\$strobe\b|\b\$monitor\b", "Simulation-only system task."),
    (r"\breal\b|\bshortreal\b", "Real-number types are not synthesizable."),
    (r"\bfork\b", "fork/join is typically not synthesizable."),
    (r"\bimport\b\s+\"DPI", "DPI is not synthesizable."),
]

def _scan_rtl_for_redflags(rtl_files: List[str], max_findings_per_file: int = 50) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
            for pat, msg in UNSYNTH_PATTERNS:
                rx = re.compile(pat)
                count = 0
                for i, ln in enumerate(lines, start=1):
                    if rx.search(ln):
                        findings.append({
                            "file": f,
                            "line": i,
                            "pattern": pat,
                            "message": msg,
                            "snippet": ln.strip()[:200],
                        })
                        count += 1
                        if count >= max_findings_per_file:
                            break
        except Exception:
            continue
    return findings

def _intent_checks(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    clocks = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks") or []
    perf = spec.get("performance") or spec.get("timing") or {}
    area = spec.get("area") or spec.get("ppa") or {}

    if clocks:
        for c in clocks:
            if isinstance(c, dict) and c.get("name"):
                if not (c.get("frequency_mhz") or c.get("period_ns") or c.get("freq_mhz") or c.get("period")):
                    findings.append({"type":"timing_intent","severity":"info",
                                     "message": f"Clock '{c['name']}' present but no frequency/period intent provided."})
    else:
        findings.append({"type":"timing_intent","severity":"info",
                         "message":"No clocks found in spec. If synchronous, include clock intent (name + freq/period)."})

    if not perf:
        findings.append({"type":"perf_intent","severity":"info",
                         "message":"No performance/timing intent section found (latency/throughput/constraints)."})
    if not area:
        findings.append({"type":"area_intent","severity":"info",
                         "message":"No area/gatecount intent found. Provide rough bounds if needed."})
    return findings

def _yosys_synth(workflow_dir: str, top: str, rtl_files: List[str]) -> Dict[str, Any]:
    if not _which("yosys"):
        return {"available": False, "note": "yosys not found in PATH"}

    synth_dir = os.path.join(workflow_dir, "synth")
    os.makedirs(synth_dir, exist_ok=True)

    script = []
    script.append("read_verilog -sv " + " ".join([f'"{p}"' for p in rtl_files]))
    script.append(f"hierarchy -check -top {top}")
    script.append("proc; opt; fsm; opt; memory; opt")
    script.append("check")
    script.append(f"synth -top {top}")
    script.append("stat")
    ys = "\n".join(script) + "\n"
    _write(os.path.join(synth_dir, "synth.ys"), ys)

    res = _run(["yosys", "-q", "-l", "yosys_synth.log", "synth.ys"], cwd=synth_dir, timeout=600)
    log_path = os.path.join(synth_dir, "yosys_synth.log")
    log_txt = ""
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            log_txt = f.read()

    warnings = [ln for ln in log_txt.splitlines() if "Warning:" in ln][:200]
    errors = [ln for ln in log_txt.splitlines() if "ERROR:" in ln][:200]
    return {"available": True, "run": res, "warnings": warnings, "errors": errors, "log_path": log_path, "script": ys}

def run_agent(state: dict) -> dict:
    agent_name = "Synthesis Readiness Agent"
    print("\n🏗️ Running Synthesis Readiness Agent (Yosys)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    top = _pick_top(spec, rtl_files, state.get("top_module"))

    redflags = _scan_rtl_for_redflags(rtl_files)
    intent = _intent_checks(spec)
    yosys = _yosys_synth(workflow_dir, top, rtl_files)

    score = 100
    score -= min(40, len(redflags))
    if yosys.get("available") and yosys.get("errors"):
        score -= 40
    if not yosys.get("available"):
        score -= 10
    score -= 5 * len(intent)
    score = max(0, score)

    findings = {
        "type": "synthesis_readiness",
        "version": "1.0",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "score": score,
        "intent_findings": intent,
        "synthesizable_subset_findings": redflags[:500],
        "yosys": {
            "available": yosys.get("available", False),
            "errors": yosys.get("errors", []),
            "warnings": yosys.get("warnings", []),
            "log_path": yosys.get("log_path"),
        },
        "tools_detected": {
            "yosys": bool(_which("yosys")),
            "iverilog": bool(_which("iverilog")),
            "verilator": bool(_which("verilator")),
        },
    }

    md = [f"# Synthesis Readiness Report\n",
          f"- Top: `{top}`",
          f"- RTL files: `{len(rtl_files)}`",
          f"- Score (heuristic): **{score}/100**\n",
          "## Timing/Area Intent Checks"]
    for it in intent:
        md.append(f"- ({it['severity']}) {it['message']}")
    md.append("\n## Synthesizable Subset Red Flags")
    if redflags:
        md.append(f"Found **{len(redflags)}** potential issues (showing up to 30):\n")
        for f in redflags[:30]:
            md.append(f"- `{os.path.relpath(f['file'])}:{f['line']}` — {f['message']}  \n  `...{f['snippet']}...`")
    else:
        md.append("No obvious red flags found by regex scan.")
    md.append("\n## Yosys Synthesis Check")
    if yosys.get("available"):
        md.append(f"- Return code: `{yosys.get('run',{}).get('returncode')}`")
        if yosys.get("errors"):
            md.append("### Errors (first 20)")
            md.extend([f"- {ln}" for ln in yosys["errors"][:20]])
        else:
            md.append("- No Yosys ERROR lines detected.")
        if yosys.get("warnings"):
            md.append("\n### Warnings (first 20)")
            md.extend([f"- {ln}" for ln in yosys["warnings"][:20]])
    else:
        md.append("- Yosys not available. Install `yosys` to enable synthesis checks.")
    report_md = "\n".join(md) + "\n"

    out_root = os.path.join(workflow_dir, "signoff")
    os.makedirs(out_root, exist_ok=True)
    _write(os.path.join(out_root, "synthesis_readiness_report.md"), report_md)
    _write(os.path.join(out_root, "synthesis_readiness_findings.json"), json.dumps(findings, indent=2))

    artifacts = {}
    artifacts["report_md"] = _record(workflow_id, agent_name, "signoff", "synthesis_readiness_report.md", report_md)
    artifacts["findings_json"] = _record(workflow_id, agent_name, "signoff", "synthesis_readiness_findings.json", json.dumps(findings, indent=2))
    if yosys.get("available") and yosys.get("log_path") and os.path.exists(yosys["log_path"]):
        with open(yosys["log_path"], "r", encoding="utf-8", errors="ignore") as f:
            artifacts["yosys_log"] = _record(workflow_id, agent_name, "signoff", "yosys_synth.log", f.read())

    findings["artifacts"] = artifacts
    state.setdefault("signoff", {})
    state["signoff"]["synthesis_readiness"] = findings
    return state
