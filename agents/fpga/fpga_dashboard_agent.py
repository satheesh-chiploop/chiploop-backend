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
    synthesis_estimate = {
        "logical_cells_used": synth.get("logical_cells_used"),
        "logical_cells_available": _first(synth.get("logical_cells_available"), ((fpga.get("target") or {}).get("resources") or {}).get("logic_cells")),
        "logic_utilization_percent": synth.get("logic_utilization_percent"),
        "flip_flops": synth.get("flip_flops"),
        "combinational_cells": synth.get("combinational_cells"),
        "lut4_cells": synth.get("lut4_cells"),
        "carry_cells": _first(synth.get("carry_cells"), (synth.get("cell_type_counts") or {}).get("SB_CARRY") if isinstance(synth.get("cell_type_counts"), dict) else None),
        "fabric_mapped_cells": synth.get("fabric_mapped_cells"),
        "total_mapped_cells": _first(synth.get("total_mapped_cells"), sum((synth.get("cell_type_counts") or {}).values()) if isinstance(synth.get("cell_type_counts"), dict) else None),
    }
    routed_result = {
        "logical_cells_used": pnr.get("logical_cells_used"),
        "logical_cells_available": _first(pnr.get("logical_cells_available"), synthesis_estimate.get("logical_cells_available")),
        "logic_utilization_percent": pnr.get("logic_utilization_percent"),
        "max_frequency_mhz": _first(timing.get("max_frequency_mhz"), pnr.get("max_frequency_mhz")),
        "timing_met": _first(timing.get("timing_met"), pnr.get("timing_met")),
        "wns_ns": _first(timing.get("wns_ns"), pnr.get("wns_ns")),
        "tns_ns": _first(timing.get("tns_ns"), pnr.get("tns_ns")),
        "timing_violation_count": _first(timing.get("timing_violation_count"), pnr.get("timing_violation_count")),
        "warning_count": _first(timing.get("warning_count"), pnr.get("warnings")),
        "error_count": _first(timing.get("error_count"), pnr.get("errors")),
    }
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
        "max_frequency_mhz": routed_result.get("max_frequency_mhz"),
        "timing_met": routed_result.get("timing_met"),
        "wns_ns": routed_result.get("wns_ns"),
        "tns_ns": routed_result.get("tns_ns"),
        "timing_violation_count": routed_result.get("timing_violation_count"),
    }
    summary = {
        "type": "fpga_dashboard",
        "status": "completed" if fpga.get("bitstream", {}).get("status") == "completed" else "review",
        "target": fpga.get("target", {}),
        "top_module": fpga.get("top_module"),
        "rtl_file_count": len(fpga.get("rtl_files") or []),
        "synthesis_estimate": synthesis_estimate,
        "routed_result": routed_result,
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
