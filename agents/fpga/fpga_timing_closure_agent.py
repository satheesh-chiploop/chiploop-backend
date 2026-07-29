import hashlib
import os
import shutil
from pathlib import Path

from .fpga_common import fpga_dir, manifest_update, publish_json


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _sha256(path: str | None) -> str | None:
    if not path or not os.path.exists(str(path)):
        return None
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_winning_output(state: dict, source: str | None, candidate: dict) -> str | None:
    if not source or not os.path.exists(str(source)):
        return None
    winning_dir = Path(fpga_dir(state, "closure", "winning")).resolve()
    winning_dir.mkdir(parents=True, exist_ok=True)
    iteration = int(candidate.get("iteration") or 0)
    seed = str(candidate.get("seed") if candidate.get("seed") is not None else "default")
    strategy = str(candidate.get("synthesis_strategy") or "baseline").replace("/", "_").replace("\\", "_")
    repair_tag = "rtl_repair" if candidate.get("rtl_repair_used") else "original_rtl"
    output = winning_dir / f"winning_i{iteration}_seed{seed}_{strategy}_{repair_tag}{Path(str(source)).suffix}"
    shutil.copy2(str(source), output)
    return str(output)


def run_agent(state: dict) -> dict:
    agent = "FPGA Timing Closure Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    pnr = fpga.get("place_route") if isinstance(fpga.get("place_route"), dict) else {}
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    timing = fpga.get("timing_drc") if isinstance(fpga.get("timing_drc"), dict) else {}
    target_mhz = _num(state.get("target_frequency_mhz") or fpga.get("target", {}).get("target_frequency_mhz"), 0.0)
    observed_mhz = _num(timing.get("max_frequency_mhz") or pnr.get("max_frequency_mhz"), 0.0)
    timing_met = timing.get("timing_met") if timing.get("timing_met") is not None else pnr.get("timing_met")
    if timing_met is None and target_mhz > 0 and observed_mhz > 0:
        timing_met = observed_mhz >= target_mhz
    complete = pnr.get("status") == "completed" and (timing_met is True or target_mhz <= 0 or observed_mhz >= target_mhz)
    pnr_command = pnr.get("command") if isinstance(pnr.get("command"), dict) else {}
    pnr_error = str(pnr_command.get("error") or pnr_command.get("stderr_tail") or pnr_command.get("stdout_tail") or pnr.get("error") or "")
    deterministic_tool_failure = (
        pnr_command.get("status") == "tool_unavailable"
        or pnr_command.get("returncode") == 127
        or "tool not found" in pnr_error.lower()
        or "not configured" in pnr_error.lower()
        or "unrecognised option" in pnr_error.lower()
        or "unknown option" in pnr_error.lower()
        or "invalid option" in pnr_error.lower()
    )
    iteration = int(state.get("fpga_timing_closure_iteration_index") or 0)
    evaluated_seed = pnr.get("seed") if pnr.get("seed") is not None else state.get("fpga_nextpnr_seed")
    repair_used = bool(state.get("fpga_timing_rtl_repair_used"))
    strategy = str(state.get("_fpga_active_synthesis_strategy") or ("flatten" if state.get("fpga_yosys_flatten") else "baseline"))
    pnr_output = pnr.get("pnr_output") or pnr.get("asc") or pnr.get("routed_config")

    history = state.setdefault("_fpga_timing_history", [])
    candidate = {
        "iteration": iteration,
        "seed": evaluated_seed,
        "max_frequency_mhz": observed_mhz or None,
        "target_frequency_mhz": target_mhz or None,
        "timing_met": timing_met,
        "wns_ns": timing.get("wns_ns"),
        "tns_ns": timing.get("tns_ns"),
        "synthesis_strategy": strategy,
        "rtl_repair_used": repair_used,
        "pnr_output": pnr_output,
        "yosys_command": (synthesis.get("command") or {}).get("cmd") if isinstance(synthesis.get("command"), dict) else None,
        "nextpnr_command": (pnr.get("command") or {}).get("cmd") if isinstance(pnr.get("command"), dict) else None,
        "synthesis_tool_effort": synthesis.get("tool_effort"),
        "place_route_tool_effort": pnr.get("tool_effort"),
    }
    history.append(candidate)
    best = max(history, key=lambda item: (_num(item.get("max_frequency_mhz"), -1.0), bool(item.get("timing_met"))))
    previous_best = state.get("_fpga_best_timing_result") if isinstance(state.get("_fpga_best_timing_result"), dict) else {}
    if best is candidate or not previous_best:
        winning_output = _copy_winning_output(state, pnr_output, candidate)
        best = {**candidate, "winning_pnr_output": winning_output}
        state["_fpga_best_timing_result"] = best
    else:
        best = previous_best

    before_candidates = [item for item in history if not item.get("rtl_repair_used")]
    after_candidates = [item for item in history if item.get("rtl_repair_used")]
    before_best = max(before_candidates, key=lambda item: _num(item.get("max_frequency_mhz"), -1.0)) if before_candidates else None
    after_best = max(after_candidates, key=lambda item: _num(item.get("max_frequency_mhz"), -1.0)) if after_candidates else None

    actions: list[str] = []
    if complete:
        actions.append("Timing is acceptable for the requested FPGA target; lock this implementation.")
    elif deterministic_tool_failure:
        actions.append(f"Implementation command cannot run: {pnr_error or 'nextpnr could not be resolved.'}")
        state["fpga_implementation_unavailable_reason"] = pnr_error or "nextpnr could not be resolved."
        state["fpga_timing_closure_failed"] = True
    else:
        if observed_mhz and target_mhz:
            actions.append(f"Observed Fmax is {observed_mhz:g} MHz against a {target_mhz:g} MHz target.")
        if state.get("allow_nextpnr_seed_sweep") is not False:
            next_seed = int(evaluated_seed or 1) + 1
            state["fpga_nextpnr_seed"] = next_seed
            actions.append(f"Retry nextpnr with seed {next_seed}.")
        actions.append("If implementation exploration fails, escalate to synthesis strategy and optional automatic RTL repair.")

    selected = best if best else candidate
    achievable = None if complete or not selected.get("max_frequency_mhz") else max(1.0, round(_num(selected.get("max_frequency_mhz")) * 0.9, 3))
    if achievable:
        state["fpga_relaxed_frequency_mhz"] = achievable

    implementation_lock = {
        "type": "fpga_implementation_lock",
        "status": "locked" if complete else "implementation_unavailable" if deterministic_tool_failure else "best_available",
        "target_frequency_mhz": target_mhz or None,
        "achieved_frequency_mhz": selected.get("max_frequency_mhz"),
        "selected_seed": selected.get("seed"),
        "synthesis_strategy": selected.get("synthesis_strategy"),
        "rtl_repair_used": selected.get("rtl_repair_used", False),
        "winning_pnr_output": selected.get("winning_pnr_output"),
        "winning_pnr_sha256": _sha256(selected.get("winning_pnr_output")),
        "yosys_command": selected.get("yosys_command"),
        "nextpnr_command": selected.get("nextpnr_command"),
        "synthesis_tool_effort": selected.get("synthesis_tool_effort"),
        "place_route_tool_effort": selected.get("place_route_tool_effort"),
        "board": (fpga.get("target") or {}).get("board"),
        "device": (fpga.get("target") or {}).get("device"),
        "package": (fpga.get("target") or {}).get("package"),
        "timing_met": None if deterministic_tool_failure else bool(selected.get("timing_met")),
    }
    plan = {
        "agent": agent,
        "status": "clean" if complete else "implementation_unavailable" if deterministic_tool_failure else "repair_recommended",
        "closure_complete": complete,
        "iteration": iteration,
        "closure_mode": state.get("fpga_closure_mode") or "balanced",
        "target_frequency_mhz": target_mhz or None,
        "observed_max_frequency_mhz": observed_mhz or None,
        "timing_met": timing_met,
        "selected_restart_stage": None if complete or deterministic_tool_failure else "FPGA nextpnr Place & Route Agent",
        "actions": actions,
        "selected_seed": selected.get("seed"),
        "selected_max_frequency_mhz": selected.get("max_frequency_mhz"),
        "selected_synthesis_strategy": selected.get("synthesis_strategy"),
        "winning_pnr_output": selected.get("winning_pnr_output"),
        "rtl_repair_enabled": bool(state.get("allow_automatic_rtl_timing_repair")),
        "rtl_repair_used": repair_used,
        "before_rtl_repair": before_best,
        "after_rtl_repair": after_best,
        "recommended_achievable_frequency_mhz": achievable,
        "settings_for_next_iteration": {
            "fpga_nextpnr_seed": state.get("fpga_nextpnr_seed"),
            "fpga_nextpnr_timing_driven": True,
        },
    }
    chart = {"target_frequency_mhz": target_mhz or None, "iterations": list(history)}
    publish_json(state, agent, "closure", "fpga_timing_closure_plan.json", plan)
    publish_json(state, agent, "closure", "fpga_timing_closure_chart.json", chart)
    publish_json(state, agent, "closure/winning", "fpga_implementation_lock.json", implementation_lock)
    manifest_update(state, "timing_closure", {"plan": plan, "chart": chart, "implementation_lock": implementation_lock})
    return state