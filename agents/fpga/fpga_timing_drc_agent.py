import os
import re
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, read_text, run_cmd


def _timing_metrics(target_mhz, observed_mhz, timing_met) -> dict:
    try:
        target = float(target_mhz or 0)
        observed = float(observed_mhz or 0)
    except Exception:
        target = 0
        observed = 0
    out = {"wns_ns": None, "tns_ns": None, "timing_violation_count": 0, "timing_met": timing_met}
    if target > 0 and observed > 0:
        target_period = 1000.0 / target
        observed_period = 1000.0 / observed
        wns = round(target_period - observed_period, 3)
        out["wns_ns"] = wns
        out["tns_ns"] = 0 if wns >= 0 else wns
        out["timing_violation_count"] = 0 if wns >= 0 else 1
        out["timing_met"] = wns >= 0
    elif timing_met is False:
        out["timing_violation_count"] = 1
    return out


def _parse_icetime_report(text: str) -> dict:
    out = {
        "max_frequency_mhz": None,
        "wns_ns": None,
        "tns_ns": None,
        "timing_violation_count": None,
    }
    freq = re.findall(r"(?:Max frequency|fmax|Timing estimate).*?([0-9]+(?:\.[0-9]+)?)\s*MHz", text, flags=re.IGNORECASE)
    if freq:
        out["max_frequency_mhz"] = float(freq[-1])
    slack_values = [
        float(value)
        for value in re.findall(r"(?:slack|WNS).*?(-?[0-9]+(?:\.[0-9]+)?)\s*ns", text, flags=re.IGNORECASE)
    ]
    if slack_values:
        out["wns_ns"] = round(min(slack_values), 3)
        out["timing_violation_count"] = sum(1 for value in slack_values if value < 0)
        out["tns_ns"] = round(sum(value for value in slack_values if value < 0), 3)
    return {key: value for key, value in out.items() if value is not None}


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
        parsed = _parse_icetime_report(text)
        summary.update(parsed)
        summary.update(_timing_metrics(state.get("target_frequency_mhz"), summary.get("max_frequency_mhz"), summary.get("timing_met")))
        summary.update({key: value for key, value in parsed.items() if value is not None})
    else:
        summary["error"] = "No routed ASC available for timing analysis."
    publish_json(state, agent, "reports", "fpga_timing_drc_summary.json", summary)
    manifest_update(state, "timing_drc", summary)
    return state
