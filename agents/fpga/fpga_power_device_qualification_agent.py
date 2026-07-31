from .fpga_common import board_config, manifest_update, publish_json


AGENT_NAME = "FPGA Power and Device Qualification Agent"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    synth = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    pnr = fpga.get("place_route") if isinstance(fpga.get("place_route"), dict) else {}
    board = board_config(state)
    resources = board.get("resources") if isinstance(board.get("resources"), dict) else {}
    used = _number(pnr.get("logical_cells_used"), _number(synth.get("logical_cells_used")))
    available = _number(pnr.get("logical_cells_available"), _number(synth.get("logical_cells_available"), _number(resources.get("logic_cells"))))
    utilization = _number(pnr.get("logic_utilization_percent"), (used / available * 100.0) if available else 0.0)
    headroom = max(0.0, 100.0 - utilization) if available else None
    ff = _number(synth.get("flip_flops"))
    frequency = _number(state.get("target_frequency_mhz") or (fpga.get("target") or {}).get("target_frequency_mhz"))
    activity = min(max(_number(state.get("fpga_activity_factor"), 0.125), 0.0), 1.0)
    # Transparent early-stage estimate; not a vendor-characterized signoff value.
    dynamic_mw = round((used * 0.0025 + ff * 0.0012) * max(frequency, 1.0) * activity, 2)
    static_mw = round(max(15.0, available * 0.0015), 2) if available else None
    estimated_mw = round(dynamic_mw + static_mw, 2) if static_mw is not None else None
    issues = []
    if not available:
        issues.append("Device capacity is unavailable.")
    if utilization >= 90:
        issues.append("Logic utilization leaves less than 10% implementation headroom.")
    if pnr.get("status") not in {"completed", "warning"}:
        issues.append("A completed place-and-route result is required for routed qualification.")
    status = "fail" if utilization > 100 else "review" if issues else "pass"
    recommendation = "qualified" if status == "pass" else "qualified_with_review" if status == "review" else "does_not_fit"
    summary = {
        "agent": AGENT_NAME,
        "status": status,
        "tool": "ChipLoop transparent activity-based estimate",
        "estimate_class": "early_stage_not_vendor_signoff",
        "board": board.get("board"),
        "board_label": board.get("label"),
        "vendor": board.get("vendor"),
        "family": board.get("family"),
        "device": board.get("device"),
        "package": board.get("package"),
        "segments": board.get("segments") or [],
        "support_tier": board.get("support_tier") or "production",
        "target_frequency_mhz": frequency or None,
        "logic_cells_used": int(used),
        "logic_cells_available": int(available) if available else None,
        "logic_utilization_percent": round(utilization, 3),
        "resource_headroom_percent": round(headroom, 3) if headroom is not None else None,
        "activity_factor": activity,
        "estimated_dynamic_power_mw": dynamic_mw,
        "estimated_static_power_mw": static_mw,
        "estimated_total_power_mw": estimated_mw,
        "qualification": recommendation,
        "issues": issues,
        "note": "Use vendor power analysis with voltage, temperature, clocks, I/O loading, and switching activity before production release.",
    }
    publish_json(state, AGENT_NAME, "qualification", "fpga_power_device_qualification_summary.json", summary)
    manifest_update(state, "power_device_qualification", summary)
    state["fpga_power_device_qualification"] = summary
    return state
