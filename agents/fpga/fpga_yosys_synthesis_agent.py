import os
from .fpga_common import fpga_dir, manifest_update, run_cmd, write_json, write_text


def run_agent(state: dict) -> dict:
    agent = "FPGA Yosys Synthesis Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "synth")
    rtl_files = fpga.get("rtl_files") or []
    top = fpga.get("top_module") or state.get("top_module")
    json_path = os.path.abspath(f"{out_dir}/{top or 'top'}_ice40.json")
    script_path = os.path.abspath(f"{out_dir}/synth_ice40.ys")
    log_path = os.path.abspath(f"{out_dir}/yosys_synth.log")
    summary = {
        "agent": agent,
        "status": "blocked",
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "json_netlist": json_path,
        "closure_iteration": int(state.get("fpga_synthesis_closure_iteration_index") or 0),
        "flatten_enabled": bool(state.get("fpga_yosys_flatten")),
    }
    if not rtl_files or not top:
        summary["error"] = "Missing RTL files or top module from FPGA handoff ingest."
        write_json(f"{out_dir}/fpga_synthesis_summary.json", summary)
        state["status"] = summary["error"]
        return state
    steps = [f"read_verilog -sv {path}" for path in rtl_files]
    if state.get("fpga_yosys_flatten"):
        steps.append("hierarchy -check")
        steps.append("flatten")
    steps.append(f"synth_ice40 -top {top} -json {json_path}")
    script = "\n".join(steps) + "\n"
    write_text(script_path, script)
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=600)
    summary.update({"status": "completed" if result["ok"] and os.path.exists(json_path) else "failed", "command": result})
    if not os.path.exists(json_path):
        summary["error"] = "Yosys did not produce the FPGA JSON netlist."
    write_json(f"{out_dir}/fpga_synthesis_summary.json", summary)
    manifest_update(state, "synthesis", summary)
    manifest_update(state, "yosys_json", json_path if os.path.exists(json_path) else None)
    if summary["status"] == "failed":
        state["status"] = "FPGA synthesis failed."
    return state
