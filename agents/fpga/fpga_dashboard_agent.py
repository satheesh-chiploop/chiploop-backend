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
        "place_route": fpga.get("place_route", {}),
        "timing_drc": fpga.get("timing_drc", {}),
        "bitstream": fpga.get("bitstream", {}),
    }
    write_json(f"{fpga_dir(state)}/fpga_dashboard.json", summary)
    return state
