from .fpga_common import fpga_dir, manifest_update, write_json


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def run_agent(state: dict) -> dict:
    agent = "FPGA Timing Closure Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "closure")
    pnr = fpga.get("place_route") if isinstance(fpga.get("place_route"), dict) else {}
    timing = fpga.get("timing_drc") if isinstance(fpga.get("timing_drc"), dict) else {}
    target_mhz = _num(state.get("target_frequency_mhz") or fpga.get("target", {}).get("target_frequency_mhz"), 0.0)
    observed_mhz = _num(pnr.get("max_frequency_mhz") or timing.get("max_frequency_mhz"), 0.0)
    timing_met = pnr.get("timing_met")
    if timing_met is None and target_mhz > 0 and observed_mhz > 0:
        timing_met = observed_mhz >= target_mhz
    complete = pnr.get("status") == "completed" and (timing_met is True or target_mhz <= 0 or observed_mhz >= target_mhz)
    iteration = int(state.get("fpga_timing_closure_iteration_index") or 0)
    next_seed = int(state.get("fpga_nextpnr_seed") or 1)
    actions: list[str] = []
    if complete:
        actions.append("Timing is acceptable for the requested FPGA target.")
    else:
        if observed_mhz and target_mhz:
            actions.append(f"Observed Fmax is {observed_mhz:g} MHz against a {target_mhz:g} MHz target.")
        if state.get("allow_nextpnr_seed_sweep") is not False:
            next_seed += 1
            state["fpga_nextpnr_seed"] = next_seed
            actions.append(f"Retry nextpnr with seed {next_seed}.")
        actions.append("If timing still fails, reduce combinational depth, pipeline critical paths, or relax the board clock target.")
        if state.get("allow_frequency_relaxation") and observed_mhz:
            state["fpga_relaxed_frequency_mhz"] = max(1.0, round(observed_mhz * 0.9, 3))
            actions.append(f"Optional relaxed target candidate: {state['fpga_relaxed_frequency_mhz']} MHz.")
    plan = {
        "agent": agent,
        "status": "clean" if complete else "repair_recommended",
        "closure_complete": complete,
        "iteration": iteration,
        "target_frequency_mhz": target_mhz or None,
        "observed_max_frequency_mhz": observed_mhz or None,
        "timing_met": timing_met,
        "selected_restart_stage": None if complete else "FPGA nextpnr Place & Route Agent",
        "actions": actions,
        "settings_for_next_iteration": {
            "fpga_nextpnr_seed": state.get("fpga_nextpnr_seed"),
            "fpga_nextpnr_timing_driven": True,
            "fpga_relaxed_frequency_mhz": state.get("fpga_relaxed_frequency_mhz"),
        },
    }
    chart = {
        "target_frequency_mhz": target_mhz or None,
        "iterations": [
            {
                "iteration": iteration,
                "max_frequency_mhz": observed_mhz or None,
                "timing_met": timing_met,
                "seed": state.get("fpga_nextpnr_seed"),
            }
        ],
    }
    write_json(f"{out_dir}/fpga_timing_closure_plan.json", plan)
    write_json(f"{out_dir}/fpga_timing_closure_chart.json", chart)
    manifest_update(state, "timing_closure", {"plan": plan, "chart": chart})
    return state
