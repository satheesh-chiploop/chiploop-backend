import json
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Verification Plan Update Agent"


def _lines(items: List[Dict[str, Any]], prefix: str) -> List[str]:
    out: List[str] = []
    for idx, item in enumerate(items[:20], start=1):
        gap_type = str(item.get("type") or "coverage_gap")
        point = str(item.get("coverage_point") or item.get("signal") or gap_type)
        rec = str(item.get("recommendation") or "Add targeted verification stimulus/checking.")
        out.append(f"- {prefix}{idx:02d}: `{point}` ({gap_type}) - {rec}")
    return out


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    iteration = int(state.get("closure_iteration_index") or 1)
    gap = state.get("coverage_gap_analysis") if isinstance(state.get("coverage_gap_analysis"), dict) else {}
    triage = state.get("failure_triage") if isinstance(state.get("failure_triage"), dict) else {}
    source_plan = state.get("source_verification_plan") if isinstance(state.get("source_verification_plan"), str) else ""
    gaps = gap.get("gaps") if isinstance(gap.get("gaps"), list) else []
    failures = triage.get("failures") if isinstance(triage.get("failures"), list) else []

    additions = _lines([g for g in gaps if isinstance(g, dict)], "VCOV-")
    failure_lines = [
        f"- VFAIL-{idx:02d}: replay `{item.get('testcase')}` seed `{item.get('seed')}` with waveform and preserve logs."
        for idx, item in enumerate([f for f in failures if isinstance(f, dict)][:20], start=1)
    ]
    base = source_plan.strip() or "# Verification Plan\n\n- Source: generated_by_closure_loop\n"
    updated = "\n".join([
        base.rstrip(),
        "",
        f"## Closure Iteration {iteration} Additions",
        "",
        "### Coverage-Driven Objectives",
        *(additions or ["- No new coverage-driven objectives were required."]),
        "",
        "### Failure Replay Objectives",
        *(failure_lines or ["- No failing testcase/seed pairs were present."]),
        "",
        "### Guardrails",
        "- Do not edit RTL in closure iterations unless failure triage classifies a true design bug and user approval is captured.",
        "- Every added testcase or seed must map to a coverage gap, failed seed, or missing checker objective.",
        "",
    ])

    report = {
        "type": "verification_plan_update",
        "iteration": iteration,
        "source_plan_present": bool(source_plan.strip()),
        "coverage_objectives_added": len(additions),
        "failure_replay_objectives_added": len(failure_lines),
        "verification_plan_path": f"verify_closure/iteration_{iteration:03d}/verification_plan_v{iteration + 1}.md",
    }
    iter_dir = out_dir / f"iteration_{iteration:03d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    plan_path = iter_dir / f"verification_plan_v{iteration + 1}.md"
    report_path = iter_dir / "verification_plan_update.json"
    plan_path.write_text(updated, encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", plan_path.name, updated)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", report_path.name, json.dumps(report, indent=2))

    state["verification_plan"] = updated
    state["closure_verification_plan_update"] = report
    return state
