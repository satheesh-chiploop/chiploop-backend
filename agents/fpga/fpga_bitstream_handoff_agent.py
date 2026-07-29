import hashlib
import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd


def _sha256(path: str) -> str | None:
    if not path or not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hardware_launch(board: dict, bitstream: str, programming_command: str | None, state: dict) -> dict:
    ready = bool(bitstream and os.path.exists(bitstream))
    expected = str(state.get("hardware_expected_behavior") or "").strip() or "Observe the behavior described in the design intent on the board I/O. Hardware confirmation is required."
    steps = [
        "Connect the selected board to the local machine over its programming USB/JTAG interface.",
        "Confirm the board is detected by openFPGALoader.",
        "Run the generated programming command with the downloaded bitstream." if programming_command else "Use the board-specific programmer; an automatic command is not available for this target.",
        "Check the expected behavior, then record whether the hardware test passed.",
    ]
    return {
        "status": "ready_for_hardware_test" if ready else "not_ready",
        "board": board.get("board"), "board_label": board.get("label"),
        "bitstream": bitstream if ready else None,
        "bitstream_filename": os.path.basename(bitstream) if ready else None,
        "bitstream_sha256": _sha256(bitstream) if ready else None,
        "programming_command": programming_command,
        "programmer_board": board.get("programmer_board"),
        "connection_steps": steps, "expected_behavior": expected,
        "programming_note": str(board.get("programming_note") or "").strip() or None,
        "confirmation_required": True,
    }

def run_agent(state: dict) -> dict:
    agent = "FPGA Bitstream Handoff Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "bitstream")
    board = board_config(state)
    family = str(board.get("family") or "ice40").lower()
    best = state.get("_fpga_best_timing_result") if isinstance(state.get("_fpga_best_timing_result"), dict) else {}
    routed_output = best.get("winning_pnr_output") if best.get("timing_met") else None
    routed_output = routed_output or (fpga.get("asc") if family == "ice40" else fpga.get("routed_config"))
    routed_output = routed_output or fpga.get("pnr_output")
    ext = str(board.get("bitstream_ext") or (".bit" if family == "ecp5" else ".bin"))
    bitstream = os.path.abspath(f"{out_dir}/{fpga.get('top_module') or 'top'}{ext}")
    summary = {
        "agent": agent,
        "status": "blocked",
        "planned_bitstream": bitstream,
        "bitstream": None,
        "artifact_produced": False,
        "target": board,
    }
    if state.get("fpga_implementation_unavailable_reason"):
        summary["error"] = f"Implementation tool unavailable; bitstream generation was not attempted. {state.get('fpga_implementation_unavailable_reason')}"
    elif state.get("fpga_timing_closure_failed"):
        summary["error"] = "Timing closure did not meet the requested frequency; bitstream generation is blocked. Review the achievable-clock recommendation or escalate the critical path."
    elif routed_output and os.path.exists(str(routed_output)):
        if family == "ecp5":
            cmd = ["ecppack", str(routed_output), bitstream]
            log_name = "ecppack.log"
        elif family == "nexus":
            cmd = ["prjoxide", "pack", str(routed_output), bitstream]
            log_name = "prjoxide_pack.log"
        elif family == "gowin":
            cmd = ["gowin_pack", "-d", str(board.get("apicula_family")), "-o", bitstream, str(routed_output)]
            log_name = "gowin_pack.log"
        else:
            cmd = ["icepack", str(routed_output), bitstream]
            log_name = "icepack.log"
        result = run_cmd(cmd, cwd=out_dir, log_path=os.path.abspath(f"{out_dir}/{log_name}"), timeout=300, state=state)
        produced = os.path.exists(bitstream)
        summary.update({
            "status": "completed" if result["ok"] and produced else "warning" if produced else "failed",
            "command": result,
            "artifact_produced": produced,
            "bitstream": bitstream if produced else None,
        })
    else:
        summary["error"] = "No routed place-and-route artifact available for bitstream generation."
    summary["programming_command"] = None
    programmer_board = summary["target"].get("programmer_board")
    if os.path.exists(bitstream) and programmer_board:
        summary["programming_command"] = f"openFPGALoader -b {programmer_board} {os.path.basename(bitstream)}"
    summary["hardware_launch"] = _hardware_launch(board, bitstream, summary.get("programming_command"), state)
    publish_json(state, agent, "bitstream", "fpga_bitstream_summary.json", summary)
    manifest_update(state, "bitstream", summary)
    if summary["status"] == "failed":
        state["status"] = "FPGA bitstream generation failed."
    return state
