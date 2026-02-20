# backend/agents/digital_smoke_exec_summary_agent.py

import os
import json
from datetime import datetime
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()

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

def _read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _read_text(path: str, limit: int = 5000) -> str:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return (f.read() or "")[:limit]
    except Exception:
        pass
    return ""

def run_agent(state: dict) -> dict:
    agent_name = "Digital Smoke Executive Summary Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Best-effort inputs (don’t hard fail)
    top = state.get("top_module") or "top"

    # Typical artifacts created by your existing agents:
    # - rtl_agent_compile.log (from Digital RTL Agent) :contentReference[oaicite:6]{index=6}
    # - vv/tb/run_regression.py + JSON report (from Simulation Control Agent) :contentReference[oaicite:7]{index=7}
    preflight_path = os.path.join(workflow_dir, "vv", "smoke", "smoke_preflight.json")
    sim_report_path = os.path.join(workflow_dir, "vv", "tb", "sim_control_generation_report.json")
    compile_log_path = os.path.join(workflow_dir, "rtl_agent_compile.log")

    preflight = _read_json(preflight_path)
    sim_report = _read_json(sim_report_path)
    compile_log = _read_text(compile_log_path)

    # Decide PASS/FAIL using robust heuristics
    compile_ok = True
    if "syntax check failed" in (compile_log.lower()):
        compile_ok = False

    sim_ok = True
    # If your sim report has fields like {"returncode":0} or {"status":"pass"} we handle both
    if sim_report:
        rc = sim_report.get("returncode")
        status = (sim_report.get("status") or "").lower()
        if rc not in (None, 0) or ("fail" in status):
            sim_ok = False

    overall_pass = compile_ok and sim_ok
    verdict = "PASS ✅" if overall_pass else "FAIL ❌"

    next_step = "Run DQA next" if overall_pass else "Fix compile/sim issues and rerun Smoke"

    md = []
    md.append(f"# Smoke Executive Summary")
    md.append(f"- Generated: `{_now()}`")
    md.append(f"- Top: `{top}`")
    md.append(f"- Verdict: **{verdict}**")
    md.append("")
    md.append("## What ran")
    md.append(f"- Preflight: {'yes' if preflight else 'no'}")
    md.append(f"- Compile: {'pass' if compile_ok else 'fail'}")
    md.append(f"- Smoke sim: {'pass' if sim_ok else 'fail'}")
    if preflight:
        md.append(f"- Simulator: `{preflight.get('simulator','')}`")
        md.append(f"- Time budget: `{preflight.get('time_budget','')}`")
        md.append(f"- RTL files: `{preflight.get('rtl_file_count','')}`")
    md.append("")
    md.append("## Key notes")
    if not compile_ok:
        md.append("- Compile failed. Check `rtl_agent_compile.log`.")
    if compile_ok and not sim_ok:
        md.append("- Simulation failed. Check `vv/tb/sim_control_generation_report.json` and regression logs.")
    if overall_pass:
        md.append("- Smoke indicates the design is not obviously broken. Proceed to DQA + Verify for deeper checks.")
    md.append("")
    md.append("## Recommended next action")
    md.append(f"- **{next_step}**")

    md_text = "\n".join(md) + "\n"
    _record(workflow_id, agent_name, "vv/smoke", "SMOKE_SUMMARY.md", md_text)

    # Optional machine-readable summary
    summary = {
        "type": "digital_smoke_summary",
        "workflow_id": workflow_id,
        "top_module": top,
        "compile_ok": compile_ok,
        "sim_ok": sim_ok,
        "verdict": "pass" if overall_pass else "fail",
        "next_step": next_step,
    }
    _record(workflow_id, agent_name, "vv/smoke", "smoke_summary.json", json.dumps(summary, indent=2))

    state["smoke_summary"] = summary
    state["status"] = f"✅ Smoke summary generated ({summary['verdict']})"
    return state
