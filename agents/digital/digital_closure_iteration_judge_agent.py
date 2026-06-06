import json
from pathlib import Path
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Closure Iteration Judge Agent"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _metrics(summary: Dict[str, Any]) -> Dict[str, Any]:
    cov = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
    code = cov.get("code") if isinstance(cov.get("code"), dict) else {}
    sim = summary.get("simulation") if isinstance(summary.get("simulation"), dict) else {}
    return {
        "functional_coverage_pct": _num(cov.get("functional_coverage_pct")),
        "code_line_coverage_pct": _num(code.get("line_coverage_pct")),
        "code_branch_coverage_pct": _num(code.get("branch_coverage_pct")),
        "code_condition_coverage_pct": _num(code.get("condition_coverage_pct")),
        "code_toggle_coverage_pct": _num(code.get("toggle_coverage_pct")),
        "simulation_total": sim.get("total"),
        "simulation_pass": sim.get("pass"),
        "simulation_fail": sim.get("fail"),
    }


def _delta(after: Optional[float], before: Optional[float]) -> Optional[float]:
    if after is None or before is None:
        return None
    return round(after - before, 3)


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    iteration = int(state.get("closure_iteration_index") or 1)
    out_dir = workflow_dir / "verify_closure" / f"iteration_{iteration:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    before = state.get("source_simulation_summary_coverage") if isinstance(state.get("source_simulation_summary_coverage"), dict) else {}
    after_path = Path(str(state.get("simulation_summary_coverage_json") or workflow_dir / "vv" / "tb" / "reports" / "simulation_summary_coverage.json"))
    after = _read_json(after_path)
    before_m = _metrics(before)
    after_m = _metrics(after)
    testcase_seed = state.get("closure_testcase_seed_update") if isinstance(state.get("closure_testcase_seed_update"), dict) else {}
    coverage_update = state.get("closure_coverage_plan_update") if isinstance(state.get("closure_coverage_plan_update"), dict) else {}

    deltas = {
        key: _delta(after_m.get(key), before_m.get(key))
        for key in (
            "functional_coverage_pct",
            "code_line_coverage_pct",
            "code_branch_coverage_pct",
            "code_condition_coverage_pct",
            "code_toggle_coverage_pct",
        )
    }
    positive_delta = any(isinstance(value, (int, float)) and value > 0 for value in deltas.values())
    failures_after = after_m.get("simulation_fail")
    gap_analysis = state.get("coverage_gap_analysis") if isinstance(state.get("coverage_gap_analysis"), dict) else {}
    closed = (
        failures_after == 0
        and all(value is None or value >= 0 for value in deltas.values())
        and not gap_analysis.get("gap_count")
    )
    stop_reason = "closure_achieved" if closed else ("coverage_improved" if positive_delta else "no_measurable_improvement")
    judgement = {
        "type": "closure_iteration_judgement",
        "iteration": iteration,
        "before": before_m,
        "after": after_m,
        "delta": deltas,
        "coverage_points_added": coverage_update.get("coverage_points_added", 0),
        "testcase_intents_added": testcase_seed.get("testcase_intents_added", 0),
        "seeds_added": testcase_seed.get("seeds_added", 0),
        "continue_recommended": not closed and positive_delta,
        "stop_reason": stop_reason,
    }
    chart = {
        "type": "verification_closure_chart",
        "source_verify_workflow_id": state.get("source_verify_workflow_id"),
        "series": [
            {
                "iteration": 0,
                "label": "baseline",
                **before_m,
                "coverage_points_added": 0,
                "testcase_intents_added": 0,
                "seeds_added": 0,
            },
            {
                "iteration": iteration,
                "label": f"iteration {iteration}",
                **after_m,
                "coverage_points_added": judgement["coverage_points_added"],
                "testcase_intents_added": judgement["testcase_intents_added"],
                "seeds_added": judgement["seeds_added"],
            },
        ],
        "deltas": deltas,
        "stop_reason": stop_reason,
    }
    judgement_txt = json.dumps(judgement, indent=2)
    chart_txt = json.dumps(chart, indent=2)
    (out_dir / "closure_judgement.json").write_text(judgement_txt, encoding="utf-8")
    (out_dir / "closure_chart.json").write_text(chart_txt, encoding="utf-8")
    (workflow_dir / "verify_closure").mkdir(parents=True, exist_ok=True)
    (workflow_dir / "verify_closure" / "closure_chart.json").write_text(chart_txt, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "closure_judgement.json", judgement_txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "closure_chart.json", chart_txt)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "verify_closure", "closure_chart.json", chart_txt)
    state["closure_iteration_judgement"] = judgement
    state["closure_chart"] = chart
    return state
