from .fpga_common import publish_json


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    synth = fpga.get("synthesis", {}) if isinstance(fpga.get("synthesis"), dict) else {}
    pnr = fpga.get("place_route", {}) if isinstance(fpga.get("place_route"), dict) else {}
    timing = fpga.get("timing_drc", {}) if isinstance(fpga.get("timing_drc"), dict) else {}
    utilization = {
        "logical_cells_used": _first(pnr.get("logical_cells_used"), synth.get("logical_cells_used")),
        "logical_cells_available": _first(pnr.get("logical_cells_available"), synth.get("logical_cells_available"), ((fpga.get("target") or {}).get("resources") or {}).get("logic_cells")),
        "logic_utilization_percent": _first(pnr.get("logic_utilization_percent"), synth.get("logic_utilization_percent")),
        "flip_flops": synth.get("flip_flops"),
        "combinational_cells": synth.get("combinational_cells"),
        "lut4_cells": synth.get("lut4_cells"),
    }
    timing_summary = {
        "target_frequency_mhz": state.get("target_frequency_mhz") or (fpga.get("target") or {}).get("target_frequency_mhz"),
        "max_frequency_mhz": _first(pnr.get("max_frequency_mhz"), timing.get("max_frequency_mhz")),
        "timing_met": _first(pnr.get("timing_met"), timing.get("timing_met")),
        "wns_ns": timing.get("wns_ns"),
        "tns_ns": timing.get("tns_ns"),
        "timing_violation_count": timing.get("timing_violation_count"),
    }
    summary = {
        "type": "fpga_dashboard",
        "status": "completed" if fpga.get("bitstream", {}).get("status") == "completed" else "review",
        "target": fpga.get("target", {}),
        "top_module": fpga.get("top_module"),
        "rtl_file_count": len(fpga.get("rtl_files") or []),
        "utilization": utilization,
        "timing_summary": timing_summary,
        "synthesis": synth,
        "synthesis_closure": fpga.get("synthesis_closure", {}),
        "place_route": pnr,
        "timing_drc": timing,
        "timing_closure": fpga.get("timing_closure", {}),
        "bitstream": fpga.get("bitstream", {}),
        "smart_context": {
            "enabled": bool(state.get("smart_context_enabled") or str(state.get("context_mode") or "").lower() == "smart"),
            "mode": state.get("context_mode") or "smart",
        },
        "hem": {
            "enabled": bool(state.get("hem_enabled")),
            "mode": state.get("hem_mode") or "fixed",
            "policy": "fpga_fixed_policy_metadata",
        },
    }
    publish_json(state, "FPGA Dashboard Agent", "", "fpga_dashboard.json", summary)
    return state
