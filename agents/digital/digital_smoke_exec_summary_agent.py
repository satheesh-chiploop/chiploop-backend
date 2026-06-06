import json
import os
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
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
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

    top = state.get("top_module") or "top"

    preflight_path = os.path.join(workflow_dir, "vv", "smoke", "smoke_preflight.json")
    sim_control_path = os.path.join(workflow_dir, "vv", "tb", "sim_control_generation_report.json")
    sim_exec_path = os.path.join(workflow_dir, "vv", "tb", "reports", "simulation_execution_summary.json")
    sim_summary_path = os.path.join(workflow_dir, "vv", "tb", "reports", "simulation_summary_coverage.json")
    compile_log_path = os.path.join(workflow_dir, "rtl_agent_compile.log")

    preflight = _read_json(preflight_path)
    sim_control = _read_json(sim_control_path)
    sim_exec = _read_json(sim_exec_path)
    sim_summary = _read_json(sim_summary_path)
    compile_log = _read_text(compile_log_path)

    compile_ok = "syntax check failed" not in compile_log.lower()

    total_runs = int(sim_exec.get("total") or 0) if sim_exec else 0
    pass_runs = int(sim_exec.get("pass") or 0) if sim_exec else 0
    fail_runs = int(sim_exec.get("fail") or 0) if sim_exec else 0

    if sim_exec:
        sim_status = "pass" if total_runs > 0 and pass_runs == total_runs and fail_runs == 0 else "fail"
    elif sim_control:
        sim_status = "not_run"
    else:
        sim_status = "missing_control"

    sim_ok = sim_status == "pass"
    if not sim_exec:
        verdict_key = "not_run"
    elif compile_ok and sim_ok:
        verdict_key = "pass"
    else:
        verdict_key = "fail"

    verdict = {"pass": "PASS", "fail": "FAIL", "not_run": "NOT RUN"}[verdict_key]
    next_step = "Run DQA next" if verdict_key == "pass" else "Fix smoke execution issues and rerun Smoke"

    md = [
        "# Smoke Executive Summary",
        f"- Generated: `{_now()}`",
        f"- Top: `{top}`",
        f"- Verdict: **{verdict}**",
        "",
        "## What ran",
        f"- Preflight: {'yes' if preflight else 'no'}",
        f"- Compile: {'pass' if compile_ok else 'fail'}",
        f"- Simulation control: {'generated' if sim_control else 'missing'}",
        f"- Smoke sim: {sim_status}",
        f"- Simulation runs: `{total_runs}`",
        f"- Simulation pass/fail: `{pass_runs}/{fail_runs}`",
    ]
    if preflight:
        md.extend([
            f"- Simulator: `{preflight.get('simulator', '')}`",
            f"- Time budget: `{preflight.get('time_budget', '')}`",
            f"- RTL files: `{preflight.get('rtl_file_count', '')}`",
        ])
    md.extend(["", "## Key notes"])
    if not compile_ok:
        md.append("- Compile failed. Check `rtl_agent_compile.log`.")
    if not sim_exec:
        md.append("- Simulation execution did not produce `vv/tb/reports/simulation_execution_summary.json`.")
    elif compile_ok and not sim_ok:
        md.append("- Simulation failed. Check `vv/tb/reports/run_logs/` and `simulation_execution_summary.json`.")
    if verdict_key == "pass":
        md.append("- Smoke indicates the design is not obviously broken. Proceed to DQA + Verify for deeper checks.")
    md.extend(["", "## Recommended next action", f"- **{next_step}**"])

    md_text = "\n".join(md) + "\n"
    _record(workflow_id, agent_name, "vv/smoke", "SMOKE_SUMMARY.md", md_text)

    summary = {
        "type": "digital_smoke_summary",
        "workflow_id": workflow_id,
        "top_module": top,
        "compile_ok": compile_ok,
        "simulation_control_generated": bool(sim_control),
        "simulation_execution_generated": bool(sim_exec),
        "simulation_summary_generated": bool(sim_summary),
        "simulation_status": sim_status,
        "simulation_total": total_runs,
        "simulation_pass": pass_runs,
        "simulation_fail": fail_runs,
        "sim_ok": sim_ok,
        "verdict": verdict_key,
        "next_step": next_step,
    }
    _record(workflow_id, agent_name, "vv/smoke", "smoke_summary.json", json.dumps(summary, indent=2))

    state["smoke_summary"] = summary
    state["status"] = f"Smoke summary generated ({summary['verdict']})"
    return state
