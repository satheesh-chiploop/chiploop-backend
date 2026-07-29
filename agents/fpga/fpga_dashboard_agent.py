import json
import os

from .fpga_common import publish_json


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _load_json(path):
    if not path or not os.path.exists(str(path)):
        return {}
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    synth = fpga.get("synthesis", {}) if isinstance(fpga.get("synthesis"), dict) else {}
    rtl_quality = fpga.get("rtl_quality", {}) if isinstance(fpga.get("rtl_quality"), dict) else {}
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
    routed_lut4 = pnr.get("routed_lut4_cells")
    routed_ff = pnr.get("routed_flip_flops")
    routed_used_fallback = max(
        int(routed_lut4 or 0),
        int(routed_ff or 0),
    ) if routed_lut4 is not None or routed_ff is not None else None
    routed_used = _first(pnr.get("logical_cells_used"), routed_used_fallback)
    routed_available = _first(pnr.get("logical_cells_available"), synthesis_estimate.get("logical_cells_available"))
    routed_utilization = pnr.get("logic_utilization_percent")
    if routed_utilization is None and routed_used is not None and routed_available:
        routed_utilization = round((float(routed_used) / float(routed_available)) * 100.0, 3)
    routed_result = {
        "logical_cells_used": routed_used,
        "routed_lut4_cells": routed_lut4,
        "logical_cells_available": routed_available,
        "logic_utilization_percent": routed_utilization,
        "utilization_source": pnr.get("utilization_source"),
        "routed_resource": pnr.get("routed_resource"),
        "routed_flip_flops": pnr.get("routed_flip_flops"),
        "max_frequency_mhz": _first(timing.get("max_frequency_mhz"), pnr.get("max_frequency_mhz")),
        "timing_met": _first(timing.get("timing_met"), pnr.get("timing_met")),
        "wns_ns": _first(timing.get("wns_ns"), pnr.get("wns_ns")),
        "tns_ns": _first(timing.get("tns_ns"), pnr.get("tns_ns")),
        "timing_violation_count": _first(timing.get("timing_violation_count"), pnr.get("timing_violation_count")),
        "warning_count": _first(timing.get("warning_count"), pnr.get("warnings")),
        "error_count": _first(timing.get("error_count"), pnr.get("errors")),
    }
    utilization = {
        "logical_cells_used": routed_used,
        "routed_lut4_cells": routed_lut4,
        "logical_cells_available": routed_available,
        "logic_utilization_percent": routed_utilization,
        "source": pnr.get("utilization_source"),
        "routed_resource": pnr.get("routed_resource"),
        "routed_flip_flops": pnr.get("routed_flip_flops"),
    }
    verification = _load_json(state.get("simulation_summary_coverage_json"))
    formal = ((state.get("vv") or {}).get("formal") or {}) if isinstance(state.get("vv"), dict) else {}
    if isinstance(formal, dict) and formal:
        verification = {**verification, "formal": formal}
        formal_toolchain = formal.get("toolchain") if isinstance(formal.get("toolchain"), dict) else {}
        verification["toolchain"] = {
            **(verification.get("toolchain") if isinstance(verification.get("toolchain"), dict) else {}),
            **formal_toolchain,
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
        "target": {
            **(fpga.get("target", {}) if isinstance(fpga.get("target"), dict) else {}),
            "target_frequency_mhz": timing_summary.get("target_frequency_mhz"),
        },
        "top_module": fpga.get("top_module"),
        "rtl_file_count": len(fpga.get("rtl_files") or []),
        "synthesis_estimate": synthesis_estimate,
        "rtl_quality": rtl_quality,
        "routed_result": routed_result,
        "utilization": utilization,
        "timing_summary": timing_summary,
        "synthesis": synth,
        "synthesis_closure": fpga.get("synthesis_closure", {}),
        "place_route": pnr,
        "timing_drc": timing,
        "timing_closure": fpga.get("timing_closure", {}),
        "timing_rtl_repair": fpga.get("timing_rtl_repair", {}),
        "bitstream": fpga.get("bitstream", {}),
        "verification": verification,
        "participating_agents": list(dict.fromkeys(state.get("_participating_agents") or [])),
        "agent_count": len(set(state.get("_participating_agents") or [])),
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
