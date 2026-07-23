import os
from .fpga_common import board_config, fpga_dir, manifest_update, read_text, run_cmd, write_json


def run_agent(state: dict) -> dict:
    agent = "FPGA Timing & DRC Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    out_dir = fpga_dir(state, "reports")
    asc = fpga.get("asc")
    log_path = os.path.abspath(f"{out_dir}/icetime.log")
    pnr = fpga.get("place_route") if isinstance(fpga.get("place_route"), dict) else {}
    summary = {
        "agent": agent,
        "status": "blocked",
        "timing_met": pnr.get("timing_met"),
        "max_frequency_mhz": pnr.get("max_frequency_mhz"),
        "warning_count": pnr.get("warnings", 0),
        "error_count": pnr.get("errors", 0),
        "target": board,
    }
    if asc and os.path.exists(str(asc)):
        result = run_cmd(["icetime", "-d", str(board.get("device") or "hx8k"), "-m", "-r", log_path, str(asc)], cwd=out_dir, log_path=log_path, timeout=300)
        text = read_text(log_path)
        summary.update({"status": "completed" if result["ok"] else "warning", "icetime": result, "report_tail": text[-3000:]})
    else:
        summary["error"] = "No routed ASC available for timing analysis."
    write_json(f"{out_dir}/fpga_timing_drc_summary.json", summary)
    manifest_update(state, "timing_drc", summary)
    return state

