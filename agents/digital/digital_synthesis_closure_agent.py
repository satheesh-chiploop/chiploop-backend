import json
import os
from typing import Any

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital Synthesis Closure Agent"
OUT_DIR = os.path.join("digital", "synthesis_closure")


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_number(*values: Any) -> float | int | None:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except Exception:
            continue
    return None


def _numbers_for_keys(data: dict[str, Any], *needles: str) -> list[float]:
    values: list[float] = []
    for key, value in data.items():
        lowered = str(key).lower()
        if not all(needle in lowered for needle in needles):
            continue
        number = _first_number(value)
        if number is not None:
            values.append(float(number))
    return values


def _min_metric(data: dict[str, Any], explicit_keys: list[str], *needles: str) -> float | None:
    explicit = [_first_number(data.get(key)) for key in explicit_keys]
    explicit_numbers = [float(value) for value in explicit if value is not None]
    scanned = _numbers_for_keys(data, *needles)
    values = explicit_numbers + scanned
    return min(values) if values else None


def _max_metric(data: dict[str, Any], explicit_keys: list[str], *needles: str) -> float | None:
    explicit = [_first_number(data.get(key)) for key in explicit_keys]
    explicit_numbers = [float(value) for value in explicit if value is not None]
    scanned = _numbers_for_keys(data, *needles)
    values = explicit_numbers + scanned
    return max(values) if values else None


def _known_status(*values: Any) -> str:
    status = str(_first_present(*values) or "").strip().lower()
    return "" if status in {"", "none", "null", "not_produced", "not produced"} else status


def _enabled(state: dict[str, Any]) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    closure = state.get("synthesis_closure") if isinstance(state.get("synthesis_closure"), dict) else {}
    return bool(_first_present(
        closure.get("enabled"),
        state.get("run_synthesis_closure_loop"),
        toggles.get("run_synthesis_closure_loop"),
        False,
    ))


def _max_iterations(state: dict[str, Any]) -> int:
    closure = state.get("synthesis_closure") if isinstance(state.get("synthesis_closure"), dict) else {}
    raw = _first_present(closure.get("max_iterations"), state.get("max_synthesis_closure_iterations"), 1)
    try:
        return max(1, min(2, int(raw)))
    except Exception:
        return 1


def _repair_enabled(state: dict[str, Any], key: str) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    closure = state.get("synthesis_closure") if isinstance(state.get("synthesis_closure"), dict) else {}
    return bool(_first_present(closure.get(key), state.get(key), toggles.get(key), True))


def _stop_enabled(state: dict[str, Any], key: str) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    closure = state.get("synthesis_closure") if isinstance(state.get("synthesis_closure"), dict) else {}
    return bool(_first_present(closure.get(key), state.get(key), toggles.get(key), False))


def _load(workflow_dir: str) -> dict[str, dict[str, Any]]:
    return {
        "synth_metrics": _read_json(os.path.join(workflow_dir, "digital", "synth", "metrics.json")),
        "synth_summary": _read_json(os.path.join(workflow_dir, "digital", "synth", "synth_summary.json")),
        "sta_preplace": _read_json(os.path.join(workflow_dir, "digital", "sta_preplace", "metrics.json")),
        "sta_preplace_summary": _read_json(os.path.join(workflow_dir, "digital", "sta_preplace", "sta_preplace_summary.json")),
        "lec": _read_json(os.path.join(workflow_dir, "digital", "lec", "lec_summary.json")),
    }


def _timing(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    synth = artifacts.get("synth_metrics") or {}
    pre = artifacts.get("sta_preplace") or {}
    pre_summary = artifacts.get("sta_preplace_summary") or {}
    wns = _first_number(
        pre.get("worst_slack"),
        pre.get("wns"),
        _min_metric(pre, ["timing__setup__ws", "timing__setup__wns"], "setup", "ws"),
        _min_metric(pre, ["timing__setup__wns"], "setup", "wns"),
        synth.get("chiploop__sta_preplace_wns"),
        synth.get("worst_slack"),
        synth.get("wns"),
        _min_metric(synth, ["timing__setup__ws", "timing__setup__wns"], "setup", "ws"),
        _min_metric(synth, ["timing__setup__wns"], "setup", "wns"),
    )
    tns = _first_number(
        pre.get("tns"),
        _min_metric(pre, ["timing__setup__tns"], "setup", "tns"),
        synth.get("chiploop__sta_preplace_tns"),
        synth.get("tns"),
        _min_metric(synth, ["timing__setup__tns"], "setup", "tns"),
    )
    setup_violations = _first_number(
        pre.get("setup_violations"),
        pre.get("timing__setup__violation_count"),
        pre.get("timing__setup__vio__count"),
        pre.get("timing__setup__violating_paths"),
        _max_metric(pre, [], "setup", "vio"),
        synth.get("chiploop__sta_preplace_setup_violation_count"),
        synth.get("timing_violations"),
        synth.get("timing__setup_vio__count"),
        _max_metric(synth, [], "setup", "vio"),
    )
    return {
        "stage": "sta_preplace" if pre or pre_summary else "synth",
        "wns": wns,
        "tns": tns,
        "setup_violations": setup_violations,
        "status": _first_present(pre_summary.get("status"), pre.get("status")),
    }


def _plan(state: dict[str, Any], artifacts: dict[str, dict[str, Any]], agent_name: str) -> dict[str, Any]:
    timing = _timing(artifacts)
    lec = artifacts.get("lec") or {}
    lec_status = _known_status(lec.get("status"), lec.get("lec_status"))
    synth = artifacts.get("synth_summary") or {}
    synth_metrics = artifacts.get("synth_metrics") or {}
    issues: list[dict[str, Any]] = []

    if _known_status(synth.get("status"), synth_metrics.get("status")) in {"failed", "fail"}:
        issues.append({
            "type": "synthesis_failed",
            "restart_stage": "Digital Synthesis Agent",
            "severity": 95,
            "evidence": {"status": _known_status(synth.get("status"), synth_metrics.get("status"))},
        })
    if (timing.get("setup_violations") or 0) > 0 or (timing.get("wns") is not None and timing["wns"] < 0):
        issues.append({
            "type": "setup_timing",
            "restart_stage": "Digital Synthesis Agent",
            "severity": 90,
            "evidence": timing,
            "reason": "Synthesis or pre-place setup timing is not closed; rerun synthesis with timing repair before PD.",
        })
    if lec_status and lec_status not in {"pass", "ok", "clean"}:
        issues.append({
            "type": "synthesis_lec",
            "restart_stage": "Digital Logic Equivalence Check Agent",
            "severity": 80,
            "evidence": {
                "status": lec_status,
                "reason": _first_present(lec.get("failure_reason"), lec.get("reason")),
                "unproven_points": lec.get("unproven_points"),
            },
            "reason": "Synthesis LEC is not proven; repair LEC setup/model/netlist issue before trusting downstream PD.",
        })
    issues.sort(key=lambda item: -int(item.get("severity", 0)))
    timing_enabled = _repair_enabled(state, "allow_synthesis_timing_repair")
    lec_enabled = _repair_enabled(state, "allow_synthesis_lec_repair")
    stop_on_closure_failure = _stop_enabled(state, "stop_on_synthesis_closure_failure")
    stop_on_lec_failure = _stop_enabled(state, "stop_on_synthesis_lec_failure")
    actions = []
    skipped = []
    for issue in issues:
        if issue["type"] == "setup_timing" and not timing_enabled:
            skipped.append({"type": issue["type"], "reason": "synthesis_timing_repair_disabled"})
            continue
        if issue["type"] == "synthesis_lec" and not lec_enabled:
            skipped.append({"type": issue["type"], "reason": "synthesis_lec_repair_disabled"})
            continue
        actions.append({
            "type": issue["type"],
            "restart_stage": issue["restart_stage"],
            "action": "Rerun synthesis closure and then rerun LEC before downstream physical signoff." if issue["type"] != "synthesis_lec" else "Repair synthesis LEC setup/model/netlist and rerun LEC.",
            "evidence": issue.get("evidence") or {},
        })
    return {
        "type": "digital_synthesis_closure_plan",
        "agent": agent_name,
        "enabled": True,
        "status": "clean" if not issues else "planned",
        "closure_complete": not issues,
        "max_iterations": _max_iterations(state),
        "iterations_planned": 0 if not issues else 1,
        "dominant_issue": issues[0]["type"] if issues else None,
        "selected_restart_stage": issues[0]["restart_stage"] if issues else None,
        "post_synthesis_timing": timing,
        "issue_summary": issues,
        "repair_actions": actions,
        "skipped_repairs": skipped,
        "downstream_policy": {
            "continue_downstream_pd": not (issues and stop_on_closure_failure) and not (
                any(issue.get("type") == "synthesis_lec" for issue in issues) and stop_on_lec_failure
            ),
            "stop_on_synthesis_closure_failure": stop_on_closure_failure,
            "stop_on_synthesis_lec_failure": stop_on_lec_failure,
            "default_behavior": "continue_downstream_pd_signoff",
        },
        "policy": {
            "bounded_iterations": "1-2",
            "setup_timing_repair_included": timing_enabled,
            "lec_repair_included": lec_enabled,
            "advisory_by_default": True,
            "no_fake_closure": True,
        },
    }


def _chart_from_plan(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    prior = state.get("synthesis_closure_chart") if isinstance(state.get("synthesis_closure_chart"), dict) else {}
    prior_series = prior.get("series") if isinstance(prior.get("series"), list) else []
    raw_iteration = _first_present(state.get("synthesis_closure_iteration_index"), state.get("closure_iteration_index"), 0)
    try:
        iteration = max(0, int(raw_iteration))
    except Exception:
        iteration = 0
    timing = plan.get("post_synthesis_timing") if isinstance(plan.get("post_synthesis_timing"), dict) else {}
    lec_issue = next((item for item in plan.get("issue_summary") or [] if isinstance(item, dict) and item.get("type") == "synthesis_lec"), {})
    lec_evidence = lec_issue.get("evidence") if isinstance(lec_issue.get("evidence"), dict) else {}
    point = {
        "iteration": iteration,
        "label": "baseline" if iteration == 0 else f"iteration {iteration}",
        "wns": timing.get("wns"),
        "tns": timing.get("tns"),
        "setup_violations": timing.get("setup_violations"),
        "lec_status": lec_evidence.get("status") or ("pass" if not lec_issue else None),
        "lec_unproven_points": lec_evidence.get("unproven_points"),
        "dominant_issue": plan.get("dominant_issue"),
        "selected_restart_stage": plan.get("selected_restart_stage"),
    }
    if prior_series:
        series = [item for item in prior_series if not (isinstance(item, dict) and item.get("iteration") == iteration)]
        series.append(point)
        series.sort(key=lambda item: int(item.get("iteration") or 0) if isinstance(item, dict) else 0)
    else:
        series = [point]
    return {
        "type": "digital_synthesis_closure_chart",
        "metric_scope": "synthesis_pre_place",
        "series": series,
        "baseline_only": len(series) == 1,
        "no_fake_iterations": True,
        "downstream_policy": plan.get("downstream_policy") or {},
    }


def run_with_agent_name(state: dict, agent_name: str = AGENT_NAME) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    if not _enabled(state):
        plan = {
            "type": "digital_synthesis_closure_plan",
            "agent": agent_name,
            "enabled": False,
            "status": "skipped",
            "reason": "synthesis_closure_loop_not_enabled",
        }
    else:
        plan = _plan(state, _load(workflow_dir), agent_name)
    chart = _chart_from_plan(state, plan) if plan.get("enabled") else {
        "type": "digital_synthesis_closure_chart",
        "series": [],
        "status": "skipped",
        "reason": plan.get("reason"),
    }
    json_text = json.dumps(plan, indent=2)
    chart_text = json.dumps(chart, indent=2)
    md_text = "\n".join([
        "# Digital Synthesis Closure Plan",
        "",
        f"- status: `{plan.get('status')}`",
        f"- selected_restart_stage: `{plan.get('selected_restart_stage')}`",
        f"- dominant_issue: `{plan.get('dominant_issue')}`",
    ])
    with open(os.path.join(out_dir, "synthesis_closure_plan.json"), "w", encoding="utf-8") as f:
        f.write(json_text)
    with open(os.path.join(out_dir, "synthesis_closure_chart.json"), "w", encoding="utf-8") as f:
        f.write(chart_text)
    with open(os.path.join(out_dir, "synthesis_closure_plan.md"), "w", encoding="utf-8") as f:
        f.write(md_text)
    try:
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "synthesis_closure/synthesis_closure_plan.json", json_text)
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "synthesis_closure/synthesis_closure_chart.json", chart_text)
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "synthesis_closure/synthesis_closure_plan.md", md_text)
    except Exception as exc:
        print(f"{agent_name}: artifact upload failed: {exc}")
    state.setdefault("digital", {})["synthesis_closure"] = {
        "status": plan.get("status"),
        "plan": plan,
        "chart": chart,
    }
    state["synthesis_closure_chart"] = chart
    return state


def run_agent(state: dict) -> dict:
    return run_with_agent_name(state, AGENT_NAME)
