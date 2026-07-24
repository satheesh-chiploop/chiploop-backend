import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, write_text


def _starter_pcf(top_module: str, frequency_mhz: float) -> str:
    return (
        f"# ChipLoop starter PCF for {top_module}\n"
        "# Replace package pins before programming real hardware.\n"
        "# Example:\n"
        "# set_io clk 35\n"
        "# set_io reset_n 10\n"
        f"# target_frequency_mhz {frequency_mhz}\n"
    )


def _starter_lpf(top_module: str, frequency_mhz: float) -> str:
    return (
        f"# ChipLoop starter LPF for {top_module}\n"
        "# Replace package pins before programming real hardware.\n"
        "# Example for ULX3S-style boards:\n"
        "# LOCATE COMP \"clk\" SITE \"G2\";\n"
        "# IOBUF PORT \"clk\" IO_TYPE=LVCMOS33;\n"
        "# FREQUENCY PORT \"clk\" 25 MHz;\n"
        f"# target_frequency_mhz {frequency_mhz}\n"
    )


def run_agent(state: dict) -> dict:
    agent = "FPGA Constraint Setup Agent"
    out_dir = fpga_dir(state, "constraints")
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    top = fpga.get("top_module") or state.get("top_module") or "top"
    frequency = float(state.get("target_frequency_mhz") or board.get("default_frequency_mhz") or 12.0)
    fmt = str(board.get("constraint_format") or "pcf").lower()
    constraint_text = str(
        state.get("constraints_lpf")
        or state.get("lpf_text")
        or state.get("constraints_pcf")
        or state.get("pcf_text")
        or ""
    )
    source_path = state.get("lpf_path") or state.get("pcf_path")
    if not constraint_text and isinstance(source_path, str) and source_path and os.path.exists(source_path):
        with open(source_path, "r", encoding="utf-8", errors="ignore") as handle:
            constraint_text = handle.read()
    generated = False
    if not constraint_text.strip():
        constraint_text = _starter_lpf(str(top), frequency) if fmt == "lpf" else _starter_pcf(str(top), frequency)
        generated = True
    constraint_path = os.path.abspath(write_text(f"{out_dir}/{top}.{fmt}", constraint_text))
    summary = {
        "agent": agent,
        "status": "ok",
        "constraint_format": fmt,
        "constraints_generated": generated,
        "constraint_path": constraint_path,
        "pcf_path": constraint_path if fmt == "pcf" else None,
        "lpf_path": constraint_path if fmt == "lpf" else None,
        "target_frequency_mhz": frequency,
        "board": board.get("board"),
        "note": "Starter constraints are enough for dry-run/reporting, but real board programming requires valid pin assignments.",
    }
    publish_json(state, agent, "constraints", "fpga_constraints_summary.json", summary)
    manifest_update(state, "constraints_pcf", constraint_path if fmt == "pcf" else None)
    manifest_update(state, "constraints_lpf", constraint_path if fmt == "lpf" else None)
    manifest_update(state, "constraints_path", constraint_path)
    manifest_update(state, "target_frequency_mhz", frequency)
    manifest_update(state, "constraints", summary)
    return state
