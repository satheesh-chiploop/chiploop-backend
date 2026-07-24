import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, read_text, run_cmd


def _timing_metrics(target_mhz, observed_mhz, timing_met) -> dict:
    try:
        target = float(target_mhz or 0)
        observed = float(observed_mhz or 0)
    except Exception:
        target = 0
        observed = 0
    out = {"wns_ns": None, "tns_ns": None, "timing_violation_count": 0}
    if target > 0 and observed > 0:
        target_period = 1000.0 / target
        observed_period = 1000.0 / observed
        wns = round(target_period - observed_period, 3)
        out["wns_ns"] = wns
        out["tns_ns"] = 0 if wns >= 0 else wns
        out["timing_violation_count"] = 0 if wns >= 0 else 1
    elif timing_met is False:
        out["timing_violation_count"] = 1
    return out


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
        summary.update(_timing_metrics(state.get("target_frequency_mhz"), summary.get("max_frequency_mhz"), summary.get("timing_met")))
    else:
        summary["error"] = "No routed ASC available for timing analysis."
    publish_json(state, agent, "reports", "fpga_timing_drc_summary.json", summary)
    manifest_update(state, "timing_drc", summary)
    return state
