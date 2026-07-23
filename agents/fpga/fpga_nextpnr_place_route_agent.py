import os
import re
from .fpga_common import board_config, fpga_dir, manifest_update, read_text, run_cmd, write_json


def _parse_nextpnr(log: str) -> dict:
    text = read_text(log)
    out = {"timing_met": None, "max_frequency_mhz": None, "warnings": text.lower().count("warning"), "errors": text.lower().count("error")}
    freq = re.findall(r"Max frequency.*?([0-9]+(?:\.[0-9]+)?)\s*MHz", text, flags=re.IGNORECASE)
    if freq:
        out["max_frequency_mhz"] = float(freq[-1])
    if "timing met" in text.lower():
        out["timing_met"] = True
    if "failed to meet timing" in text.lower() or "timing failed" in text.lower():
        out["timing_met"] = False
    return out


def run_agent(state: dict) -> dict:
    agent = "FPGA nextpnr Place & Route Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    out_dir = fpga_dir(state, "pnr")
    json_netlist = fpga.get("yosys_json")
    pcf = fpga.get("constraints_pcf")
    asc_path = os.path.abspath(f"{out_dir}/{fpga.get('top_module') or 'top'}.asc")
    log_path = os.path.abspath(f"{out_dir}/nextpnr_ice40.log")
    seed = state.get("fpga_nextpnr_seed") or state.get("nextpnr_seed")
    summary = {
        "agent": agent,
        "status": "blocked",
        "target": board,
        "asc": asc_path,
        "closure_iteration": int(state.get("fpga_timing_closure_iteration_index") or 0),
        "seed": seed,
        "timing_driven": bool(state.get("fpga_nextpnr_timing_driven") or state.get("run_fpga_timing_closure_loop")),
    }
    if not json_netlist or not os.path.exists(str(json_netlist)):
        summary["error"] = "Missing Yosys JSON netlist."
    else:
        cmd = [
            "nextpnr-ice40",
            str(board.get("nextpnr_device_flag") or "--hx8k"),
            "--package",
            str(board.get("nextpnr_package") or board.get("package") or "ct256"),
            "--json",
            str(json_netlist),
            "--asc",
            asc_path,
        ]
        if pcf and os.path.exists(str(pcf)):
            cmd.extend(["--pcf", str(pcf)])
        if seed:
            cmd.extend(["--seed", str(seed)])
        result = run_cmd(cmd, cwd=out_dir, log_path=log_path, timeout=900)
        summary.update(_parse_nextpnr(log_path))
        summary.update({"status": "completed" if result["ok"] and os.path.exists(asc_path) else "failed", "command": result})
        if not os.path.exists(asc_path):
            summary["error"] = "nextpnr did not produce an ASC place-route output."
    write_json(f"{out_dir}/fpga_place_route_summary.json", summary)
    manifest_update(state, "place_route", summary)
    manifest_update(state, "asc", asc_path if os.path.exists(asc_path) else None)
    if summary["status"] == "failed":
        state["status"] = "FPGA place-and-route failed."
    return state
