import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd


def run_agent(state: dict) -> dict:
    agent = "FPGA Bitstream Handoff Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "bitstream")
    board = board_config(state)
    family = str(board.get("family") or "ice40").lower()
    routed_output = fpga.get("asc") if family == "ice40" else fpga.get("routed_config")
    routed_output = routed_output or fpga.get("pnr_output")
    ext = str(board.get("bitstream_ext") or (".bit" if family == "ecp5" else ".bin"))
    bitstream = os.path.abspath(f"{out_dir}/{fpga.get('top_module') or 'top'}{ext}")
    summary = {"agent": agent, "status": "blocked", "bitstream": bitstream, "target": board}
    if routed_output and os.path.exists(str(routed_output)):
        if family == "ecp5":
            cmd = ["ecppack", str(routed_output), bitstream]
            log_name = "ecppack.log"
        else:
            cmd = ["icepack", str(routed_output), bitstream]
            log_name = "icepack.log"
        result = run_cmd(cmd, cwd=out_dir, log_path=os.path.abspath(f"{out_dir}/{log_name}"), timeout=300)
        summary.update({"status": "completed" if result["ok"] and os.path.exists(bitstream) else "failed", "command": result})
    else:
        summary["error"] = "No routed place-and-route artifact available for bitstream generation."
    summary["programming_command"] = None
    programmer_board = summary["target"].get("programmer_board")
    if os.path.exists(bitstream) and programmer_board:
        summary["programming_command"] = f"openFPGALoader -b {programmer_board} {bitstream}"
    publish_json(state, agent, "bitstream", "fpga_bitstream_summary.json", summary)
    manifest_update(state, "bitstream", summary)
    if summary["status"] == "failed":
        state["status"] = "FPGA bitstream generation failed."
    return state
