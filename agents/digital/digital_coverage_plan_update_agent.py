import json
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Coverage Plan Update Agent"


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    iteration = int(state.get("closure_iteration_index") or 1)
    out_dir = workflow_dir / "verify_closure" / f"iteration_{iteration:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_plan = state.get("source_coverage_point_plan") if isinstance(state.get("source_coverage_point_plan"), str) else ""
    gap = state.get("coverage_gap_analysis") if isinstance(state.get("coverage_gap_analysis"), dict) else {}
    functional_gaps = [g for g in (gap.get("functional_gaps") or []) if isinstance(g, dict)]
    generic_gaps = [g for g in (gap.get("gaps") or []) if isinstance(g, dict)]
    combined_gaps: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*functional_gaps, *generic_gaps]:
        key = json.dumps({k: item.get(k) for k in ("type", "coverage_point", "signal", "actual", "target")}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        combined_gaps.append(item)

    added_points: List[Dict[str, Any]] = []
    for idx, item in enumerate(combined_gaps[:24], start=1):
        point = str(item.get("coverage_point") or item.get("signal") or item.get("type") or f"closure_gap_{idx}")
        added_points.append({
            "id": f"COV_ITER{iteration:03d}_{idx:03d}",
            "coverage_point": point,
            "source_gap_type": item.get("type"),
            "source": "code_coverage_gap" if str(item.get("type") or "").startswith("code_") else "functional_coverage_gap",
            "target_bins": item.get("missing_bins") or ["unhit"],
            "traceability": item,
        })

    base = source_plan.strip() or "# Coverage Point Plan\n\n- Source: generated_by_closure_loop\n"
    lines = [
        base.rstrip(),
        "",
        f"## Closure Iteration {iteration} Coverage Points",
        "",
    ]
    if added_points:
        for point in added_points:
            lines.append(f"- {point['id']}: `{point['coverage_point']}` target bins {', '.join(map(str, point['target_bins']))}")
    else:
        lines.append("- No additional coverage points were required.")
    lines.append("")
    updated = "\n".join(lines)

    report = {
        "type": "coverage_plan_update",
        "iteration": iteration,
        "source_plan_present": bool(source_plan.strip()),
        "coverage_points_added": len(added_points),
        "added_points": added_points,
        "coverage_plan_path": f"verify_closure/iteration_{iteration:03d}/coverage_point_plan_v{iteration + 1}.md",
    }
    plan_path = out_dir / f"coverage_point_plan_v{iteration + 1}.md"
    report_path = out_dir / "coverage_plan_update.json"
    plan_path.write_text(updated, encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", plan_path.name, updated)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", report_path.name, json.dumps(report, indent=2))

    state["coverage_plan"] = updated
    state["closure_coverage_plan_update"] = report
    state["closure_added_coverage_points"] = added_points
    return state
