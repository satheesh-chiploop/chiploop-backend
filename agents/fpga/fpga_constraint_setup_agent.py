import os
import re
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, write_text


def _extract_ports_from_rtl(paths: list[str]) -> list[str]:
    ports: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for match in re.finditer(r"\b(?:input|output|inout)\b(?:\s+(?:wire|reg|logic|signed))*\s*(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*)", text):
            for name in match.group(1).split(","):
                clean = re.sub(r"[^A-Za-z0-9_$].*$", "", name.strip())
                if clean and clean not in seen:
                    seen.add(clean)
                    ports.append(clean)
    return ports


def _pin_for_pcf_port(board_key: str, port: str) -> str | None:
    lower = port.lower()
    if board_key == "icebreaker":
        pins = {
            "clk": "35",
            "clock": "35",
            "clk_12mhz": "35",
            "reset": "10",
            "rst": "10",
            "reset_n": "10",
            "rst_n": "10",
            "btn": "10",
            "button": "10",
            "led": "37",
            "led_n": "37",
            "led0": "37",
            "led_0": "37",
            "led1": "11",
            "led_1": "11",
        }
        return pins.get(lower)
    if board_key == "upduino_v3":
        pins = {
            "clk": "20",
            "clock": "20",
            "reset": "19",
            "rst": "19",
            "reset_n": "19",
            "rst_n": "19",
            "led": "39",
            "led0": "39",
            "led_0": "39",
            "led1": "40",
            "led_1": "40",
        }
        return pins.get(lower)
    if board_key == "icestick":
        pins = {
            "clk": "21",
            "clock": "21",
            "reset": "44",
            "rst": "44",
            "reset_n": "44",
            "rst_n": "44",
            "led": "95",
            "led0": "99",
            "led_0": "99",
            "led1": "98",
            "led_1": "98",
        }
        return pins.get(lower)
    return None


def _starter_pcf(top_module: str, frequency_mhz: float, board_key: str, ports: list[str]) -> tuple[str, list[str]]:
    lines = [
        f"# ChipLoop starter PCF for {top_module}",
        f"# target_frequency_mhz {frequency_mhz}",
    ]
    constrained: list[str] = []
    for port in ports:
        pin = _pin_for_pcf_port(board_key, port)
        if pin:
            lines.append(f"set_io -nowarn {port} {pin}")
            constrained.append(port)
    if not constrained:
        lines.extend([
            "# No known demo pins matched this RTL. Provide board-specific PCF before programming real hardware.",
            "# Common iCEBreaker examples:",
            "# set_io -nowarn clk 35",
            "# set_io -nowarn led 37",
            "# set_io -nowarn reset_n 10",
        ])
    return "\n".join(lines).strip() + "\n", constrained


def _pin_for_lpf_port(board_key: str, port: str) -> str | None:
    if board_key != "ulx3s_ecp5_45f":
        return None
    pins = {
        "clk": "G2",
        "clock": "G2",
        "reset": "D6",
        "rst": "D6",
        "reset_n": "D6",
        "rst_n": "D6",
        "led": "B2",
        "led0": "B2",
        "led_0": "B2",
        "led1": "C2",
        "led_1": "C2",
    }
    return pins.get(port.lower())


def _starter_lpf(top_module: str, frequency_mhz: float, board_key: str, ports: list[str]) -> tuple[str, list[str]]:
    lines = [
        f"# ChipLoop starter LPF for {top_module}",
        f"# target_frequency_mhz {frequency_mhz}",
    ]
    constrained: list[str] = []
    for port in ports:
        pin = _pin_for_lpf_port(board_key, port)
        if pin:
            lines.append(f'LOCATE COMP "{port}" SITE "{pin}";')
            lines.append(f'IOBUF PORT "{port}" IO_TYPE=LVCMOS33;')
            constrained.append(port)
    if any(port.lower() in {"clk", "clock"} for port in constrained):
        clock_port = next(port for port in constrained if port.lower() in {"clk", "clock"})
        lines.append(f'FREQUENCY PORT "{clock_port}" {frequency_mhz} MHz;')
    if not constrained:
        lines.extend([
            "# No known demo pins matched this RTL. Provide board-specific LPF before programming real hardware.",
            '# LOCATE COMP "clk" SITE "G2";',
            '# IOBUF PORT "clk" IO_TYPE=LVCMOS33;',
            '# FREQUENCY PORT "clk" 25 MHz;',
        ])
    return "\n".join(lines).strip() + "\n", constrained


def run_agent(state: dict) -> dict:
    agent = "FPGA Constraint Setup Agent"
    out_dir = fpga_dir(state, "constraints")
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    board = board_config(state)
    top = fpga.get("top_module") or state.get("top_module") or "top"
    frequency = float(state.get("target_frequency_mhz") or board.get("default_frequency_mhz") or 12.0)
    fmt = str(board.get("constraint_format") or "pcf").lower()
    board_key = str(board.get("board") or state.get("board") or "custom").lower()
    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    rtl_ports = _extract_ports_from_rtl(rtl_files)
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
    constrained_ports: list[str] = []
    if not constraint_text.strip():
        if fmt == "lpf":
            constraint_text, constrained_ports = _starter_lpf(str(top), frequency, board_key, rtl_ports)
        else:
            constraint_text, constrained_ports = _starter_pcf(str(top), frequency, board_key, rtl_ports)
        generated = True
    constraint_path = os.path.abspath(write_text(f"{out_dir}/{top}.{fmt}", constraint_text))
    summary = {
        "agent": agent,
        "status": "ok",
        "constraint_format": fmt,
        "constraints_generated": generated,
        "constrained_ports": constrained_ports,
        "unconstrained_ports": [port for port in rtl_ports if port not in constrained_ports],
        "constraint_path": constraint_path,
        "pcf_path": constraint_path if fmt == "pcf" else None,
        "lpf_path": constraint_path if fmt == "lpf" else None,
        "target_frequency_mhz": frequency,
        "board": board.get("board"),
        "note": "Generated demo constraints are intended for common clock/reset/LED examples. Custom boards or interfaces should provide board-verified PCF/LPF pin assignments.",
    }
    publish_json(state, agent, "constraints", "fpga_constraints_summary.json", summary)
    manifest_update(state, "constraints_pcf", constraint_path if fmt == "pcf" else None)
    manifest_update(state, "constraints_lpf", constraint_path if fmt == "lpf" else None)
    manifest_update(state, "constraints_path", constraint_path)
    manifest_update(state, "target_frequency_mhz", frequency)
    manifest_update(state, "constraints", summary)
    return state
