import json
from pathlib import Path
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Closure Recommendation Agent"


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    out_dir = workflow_dir / "verify_closure"
    out_dir.mkdir(parents=True, exist_ok=True)

    gap = state.get("coverage_gap_analysis") if isinstance(state.get("coverage_gap_analysis"), dict) else {}
    triage = state.get("failure_triage") if isinstance(state.get("failure_triage"), dict) else {}
    gaps = gap.get("gaps") if isinstance(gap.get("gaps"), list) else []
    functional_gaps = gap.get("functional_gaps") if isinstance(gap.get("functional_gaps"), list) else []
    failures = triage.get("failures") if isinstance(triage.get("failures"), list) else []
    summary = state.get("source_simulation_summary_coverage") if isinstance(state.get("source_simulation_summary_coverage"), dict) else {}
    coverage = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
    code = coverage.get("code") if isinstance(coverage.get("code"), dict) else {}
    functional = coverage.get("functional") if isinstance(coverage.get("functional"), dict) else {}
    simulation = summary.get("simulation") if isinstance(summary.get("simulation"), dict) else {}
    formal = summary.get("formal") if isinstance(summary.get("formal"), dict) else {}
    golden = summary.get("golden_model") if isinstance(summary.get("golden_model"), dict) else {}
    toolchain = summary.get("toolchain") if isinstance(summary.get("toolchain"), dict) else {}

    actions: List[Dict[str, Any]] = []
    if failures:
        actions.append({
            "id": "rerun_failed_seeds_with_waveform",
            "priority": "critical",
            "human_approval_required": True,
            "description": "Replay failing testcase/seed pairs with waveform enabled before changing RTL.",
            "inputs": [{"testcase": f.get("testcase"), "seed": f.get("seed")} for f in failures],
        })
    if gaps:
        actions.append({
            "id": "generate_directed_tests_for_coverage_holes",
            "priority": "high",
            "human_approval_required": True,
            "description": "Generate directed tests targeting unhit functional bins and uncovered code paths.",
        })
        actions.append({
            "id": "review_unreachable_or_wrong_bins",
            "priority": "medium",
            "human_approval_required": True,
            "description": "Review whether any coverage holes are unreachable or incorrectly modeled.",
        })
    if not gaps and not failures:
        actions.append({
            "id": "no_action_required",
            "priority": "info",
            "human_approval_required": False,
            "description": "Coverage targets are met and no failing tests were found.",
        })

    verdict = "closed" if not gaps and not failures else ("debug_failures_first" if failures else "coverage_closure_needed")
    plan = {
        "type": "verify_closure_plan",
        "source_verify_workflow_id": state.get("source_verify_workflow_id"),
        "verdict": verdict,
        "coverage_gap_count": len(gaps),
        "functional_gap_count": len(functional_gaps),
        "functional_gaps": functional_gaps[:20],
        "failure_count": len(failures),
        "verify_evidence": {
            "simulation": {
                "total": simulation.get("total"),
                "pass": simulation.get("pass"),
                "fail": simulation.get("fail"),
            },
            "functional_coverage_pct": coverage.get("functional_coverage_pct") or functional.get("coverage_pct"),
            "code_coverage": {
                "line_coverage_pct": code.get("line_coverage_pct"),
                "branch_coverage_pct": code.get("branch_coverage_pct"),
                "condition_coverage_pct": code.get("condition_coverage_pct"),
                "condition_source": code.get("condition_source"),
                "toggle_coverage_pct": code.get("toggle_coverage_pct"),
                "toggle_source": code.get("toggle_source"),
            },
            "formal": formal,
            "golden_model": golden,
            "toolchain": toolchain,
        },
        "recommended_actions": actions,
        "rerun_policy": {
            "automatic_rtl_edit": False,
            "automatic_coverage_model_edit": False,
            "rerun_requires_human_approval": True,
        },
    }
    txt = json.dumps(plan, indent=2)
    md = "\n".join([
        "# Verification Closure Plan",
        "",
        f"- Verdict: {verdict}",
        f"- Coverage gaps: {len(gaps)}",
        f"- Functional bin gaps: {len(functional_gaps)}",
        f"- Failing testcase/seed pairs: {len(failures)}",
        "",
        "## Verify Evidence Snapshot",
        f"- Runs: {simulation.get('total')}",
        f"- Simulation pass/fail: {simulation.get('pass')} / {simulation.get('fail')}",
        f"- Functional coverage: {coverage.get('functional_coverage_pct') or functional.get('coverage_pct')}",
        f"- Code line/branch/condition/toggle coverage: "
        f"{code.get('line_coverage_pct')} / {code.get('branch_coverage_pct')} / "
        f"{code.get('condition_coverage_pct')} / {code.get('toggle_coverage_pct')}",
        f"- Formal: {formal.get('status')}",
        f"- Golden model: {golden.get('status')}",
        f"- Tools: {toolchain.get('simulator')} / {toolchain.get('code_coverage')} / {toolchain.get('formal')}",
        "",
        "## Functional Coverage Not Met",
        *[
            (
                f"- {item.get('coverage_point')}: bins {item.get('hit_bins')}/{item.get('total_bins')}, "
                f"missing {', '.join(item.get('missing_bins') or ['unknown'])}; "
                f"{item.get('recommendation')}"
            )
            for item in functional_gaps[:20]
        ],
        "",
        "## Recommended Actions",
        *[f"- {item['priority']}: {item['description']}" for item in actions],
    ]) + "\n"
    (out_dir / "verify_closure_plan.json").write_text(txt, encoding="utf-8")
    (out_dir / "verify_closure_plan.md").write_text(md, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "verify_closure_plan.json", txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "verify_closure_plan.md", md)
    state["verify_closure_plan"] = plan
    return state
