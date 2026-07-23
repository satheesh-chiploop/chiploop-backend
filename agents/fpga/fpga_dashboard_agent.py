from .fpga_common import fpga_dir, write_json


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    summary = {
        "type": "fpga_dashboard",
        "status": "completed" if fpga.get("bitstream", {}).get("status") == "completed" else "review",
        "target": fpga.get("target", {}),
        "top_module": fpga.get("top_module"),
        "rtl_file_count": len(fpga.get("rtl_files") or []),
        "synthesis": fpga.get("synthesis", {}),
        "synthesis_closure": fpga.get("synthesis_closure", {}),
        "place_route": fpga.get("place_route", {}),
        "timing_drc": fpga.get("timing_drc", {}),
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
    write_json(f"{fpga_dir(state)}/fpga_dashboard.json", summary)
    return state
