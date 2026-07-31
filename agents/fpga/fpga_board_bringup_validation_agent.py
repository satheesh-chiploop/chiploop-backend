import os

from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd


AGENT_NAME = "FPGA Board Bring-up and Hardware Validation Agent"


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    bitstream = fpga.get("bitstream") if isinstance(fpga.get("bitstream"), dict) else {}
    board = board_config(state)
    enabled = bool(state.get("run_fpga_hardware_validation", False))
    require_pass = bool(state.get("require_fpga_hardware_validation", False))
    program_board = bool(state.get("program_connected_fpga", False))
    artifact = str(bitstream.get("bitstream") or "")
    programmer_board = str(board.get("programmer_board") or "")
    expected = str(state.get("hardware_expected_behavior") or "").strip() or "Confirm the behavior defined by the design intent."
    out_dir = fpga_dir(state, "hardware")
    summary = {
        "agent": AGENT_NAME,
        "enabled": enabled,
        "required": require_pass,
        "tool": "openFPGALoader",
        "board": board.get("board"),
        "board_label": board.get("label"),
        "programmer_board": programmer_board or None,
        "bitstream": artifact or None,
        "expected_behavior": expected,
        "programming_requested": program_board,
        "status": "disabled",
        "checks": [],
    }
    if not enabled:
        summary["reason"] = "Hardware validation disabled; bitstream remains available for local programming."
    elif not artifact or not os.path.exists(artifact):
        summary.update(status="blocked", reason="A generated bitstream is required before board bring-up.")
    elif not programmer_board:
        summary.update(status="ready_for_manual_test", reason="This board requires a programmer-specific manual command.")
    elif not program_board:
        summary.update(
            status="ready_for_hardware_test",
            programming_command=f"openFPGALoader -b {programmer_board} {os.path.basename(artifact)}",
            reason="Connect the board and explicitly enable programming to run the hardware test.",
        )
    else:
        detect = run_cmd(["openFPGALoader", "--detect"], cwd=out_dir, log_path=os.path.join(out_dir, "board_detect.log"), timeout=60, state=state)
        summary["checks"].append({"name": "board_detected", "result": detect})
        if not detect.get("ok"):
            summary.update(status="fail", reason="The selected FPGA board was not detected.")
        else:
            program = run_cmd(["openFPGALoader", "-b", programmer_board, artifact], cwd=out_dir, log_path=os.path.join(out_dir, "board_program.log"), timeout=300, state=state)
            summary["checks"].append({"name": "programming", "result": program})
            observed = str(state.get("hardware_observed_behavior") or "").strip()
            confirmed = bool(state.get("hardware_test_passed", False))
            summary.update(
                status="pass" if program.get("ok") and confirmed else "awaiting_confirmation" if program.get("ok") else "fail",
                observed_behavior=observed or None,
                hardware_test_passed=confirmed if program.get("ok") else False,
                reason=None if program.get("ok") and confirmed else "Programming completed; record the observed behavior and confirm the smoke test." if program.get("ok") else "Board programming failed.",
            )
    publish_json(state, AGENT_NAME, "hardware", "fpga_hardware_validation_summary.json", summary)
    manifest_update(state, "hardware_validation", summary)
    state["fpga_hardware_validation"] = summary
    if require_pass and summary["status"] != "pass":
        raise RuntimeError(f"FPGA hardware validation did not pass: {summary.get('reason') or summary['status']}")
    return state
