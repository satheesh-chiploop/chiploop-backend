import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd


def run_agent(state: dict) -> dict:
    agent = "FPGA Bitstream Handoff Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "bitstream")
    asc = fpga.get("asc")
    bitstream = os.path.abspath(f"{out_dir}/{fpga.get('top_module') or 'top'}.bin")
    summary = {"agent": agent, "status": "blocked", "bitstream": bitstream, "target": board_config(state)}
    if asc and os.path.exists(str(asc)):
        result = run_cmd(["icepack", str(asc), bitstream], cwd=out_dir, log_path=os.path.abspath(f"{out_dir}/icepack.log"), timeout=300)
        summary.update({"status": "completed" if result["ok"] and os.path.exists(bitstream) else "failed", "command": result})
    else:
        summary["error"] = "No routed ASC available for bitstream generation."
    summary["programming_command"] = None
    programmer_board = summary["target"].get("programmer_board")
    if os.path.exists(bitstream) and programmer_board:
        summary["programming_command"] = f"openFPGALoader -b {programmer_board} {bitstream}"
    publish_json(state, agent, "bitstream", "fpga_bitstream_summary.json", summary)
    manifest_update(state, "bitstream", summary)
    if summary["status"] == "failed":
        state["status"] = "FPGA bitstream generation failed."
    return state
