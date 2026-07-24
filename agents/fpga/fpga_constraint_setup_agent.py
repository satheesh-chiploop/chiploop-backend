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


def run_agent(state: dict) -> dict:
    agent = "FPGA Constraint Setup Agent"
    out_dir = fpga_dir(state, "constraints")
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    top = fpga.get("top_module") or state.get("top_module") or "top"
    frequency = float(state.get("target_frequency_mhz") or board.get("default_frequency_mhz") or 12.0)
    pcf_text = str(state.get("constraints_pcf") or state.get("pcf_text") or "")
    source_pcf = state.get("pcf_path")
    if not pcf_text and isinstance(source_pcf, str) and source_pcf and os.path.exists(source_pcf):
        with open(source_pcf, "r", encoding="utf-8", errors="ignore") as handle:
            pcf_text = handle.read()
    generated = False
    if not pcf_text.strip():
        pcf_text = _starter_pcf(str(top), frequency)
        generated = True
    pcf_path = write_text(f"{out_dir}/{top}.pcf", pcf_text)
    summary = {
        "agent": agent,
        "status": "ok",
        "constraint_format": "pcf",
        "constraints_generated": generated,
        "pcf_path": pcf_path,
        "target_frequency_mhz": frequency,
        "board": board.get("board"),
        "note": "Starter constraints are enough for dry-run/reporting, but real board programming requires valid pin assignments.",
    }
    publish_json(state, agent, "constraints", "fpga_constraints_summary.json", summary)
    manifest_update(state, "constraints_pcf", pcf_path)
    manifest_update(state, "target_frequency_mhz", frequency)
    manifest_update(state, "constraints", summary)
    return state
