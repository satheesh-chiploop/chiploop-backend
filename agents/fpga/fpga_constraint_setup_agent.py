import os
import re
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, write_text


def _extract_ports_from_rtl(paths: list[str]) -> list[str]:
    def add_port(name: str) -> None:
        clean = re.sub(r"[^A-Za-z0-9_$].*$", "", name.strip())
        if clean and clean not in seen:
            seen.add(clean)
            ports.append(clean)

    ports: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            text = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for module_match in re.finditer(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*\((.*?)\)\s*;", text, flags=re.DOTALL):
            current_direction = ""
            for segment in module_match.group(1).split(","):
                tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", re.sub(r"\[[^\]]+\]", " ", segment))
                useful = [token for token in tokens if token not in {"wire", "reg", "logic", "signed", "unsigned"}]
                if not useful:
                    continue
                if useful[0] in {"input", "output", "inout"}:
                    current_direction = useful[0]
                    useful = useful[1:]
                if current_direction and useful:
                    add_port(useful[-1])
        for match in re.finditer(r"^\s*(?:input|output|inout)\b([^\n;]*);", text, flags=re.MULTILINE):
            fragment = re.sub(r"\[[^\]]+\]", " ", match.group(1))
            fragment = re.sub(r"\b(?:wire|reg|logic|signed|unsigned)\b", " ", fragment)
            for name in fragment.split(","):
                add_port(name)
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
    if board_key == "ice40_hx8k_breakout":
        pins = {
            "clk": "J3",
            "clock": "J3",
            "clk_12mhz": "J3",
            "led": "B5",
            "led0": "B5",
            "led_0": "B5",
            "led1": "B4",
            "led_1": "B4",
            "led2": "A2",
            "led_2": "A2",
            "led3": "A1",
            "led_3": "A1",
            "led4": "C5",
            "led_4": "C5",
            "led5": "C4",
            "led_5": "C4",
            "led6": "B3",
            "led_6": "B3",
            "led7": "C3",
            "led_7": "C3",
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
    if board_key == "ulx3s_ecp5_45f":
        pins = {
            "clk": "G2",
            "clock": "G2",
            "clk_25mhz": "G2",
            "reset": "T1",
            "rst": "T1",
            "reset_n": "T1",
            "rst_n": "T1",
            "led": "B2",
            "led0": "B2",
            "led_0": "B2",
            "led1": "C2",
            "led_1": "C2",
            "led2": "C1",
            "led_2": "C1",
            "led3": "D2",
            "led_3": "D2",
            "led4": "D1",
            "led_4": "D1",
            "led5": "E2",
            "led_5": "E2",
            "led6": "E1",
            "led_6": "E1",
            "led7": "H3",
            "led_7": "H3",
        }
        return pins.get(port.lower())
    if board_key == "orangecrab_ecp5_85f":
        pins = {
            "clk": "A9",
            "clock": "A9",
            "clk_48mhz": "A9",
            "reset": "J17",
            "rst": "J17",
            "button": "J17",
            "btn": "J17",
            "reset_n": "V17",
            "rst_n": "V17",
            "led": "K4",
            "led0": "K4",
            "led_0": "K4",
            "led_r": "K4",
            "led1": "M3",
            "led_1": "M3",
            "led_g": "M3",
            "led2": "J3",
            "led_2": "J3",
            "led_b": "J3",
        }
        return pins.get(port.lower())
    if board_key == "colorlight_5a_75b":
        pins = {
            "clk": "P6",
            "clock": "P6",
            "clk_25mhz": "P6",
            "reset": "P11",
            "rst": "P11",
            "button": "P11",
            "btn": "P11",
            "reset_n": "P11",
            "rst_n": "P11",
            "led": "T6",
            "led0": "T6",
            "led_0": "T6",
            "user_led": "T6",
        }
        return pins.get(port.lower())
    return None


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

def _starter_cst(top_module: str, frequency_mhz: float) -> tuple[str, list[str]]:
    return (
        f"// ChipLoop starter CST for {top_module}\n"
        f"// target_frequency_mhz {frequency_mhz}\n"
        "// Supply a board-verified Gowin CST before implementation/programming.\n",
        [],
    )


def _constrained_cst_ports(text: str) -> list[str]:
    ports: list[str] = []
    for port in re.findall(r'IO_LOC\s+"([^"]+)"', text, flags=re.IGNORECASE):
        if port not in ports:
            ports.append(port)
    return ports



def _constrained_ports_from_text(fmt: str, text: str) -> list[str]:
    ports: list[str] = []
    seen: set[str] = set()
    if fmt == "lpf":
        matches = re.findall(r'\b(?:LOCATE\s+COMP|IOBUF\s+PORT|FREQUENCY\s+PORT)\s+"([^"]+)"', text, flags=re.IGNORECASE)
    else:
        matches = re.findall(r"^\s*set_io\b(?:\s+-[A-Za-z0-9_-]+)*\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.IGNORECASE | re.MULTILINE)
    for port in matches:
        if port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


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
        state.get("constraints_cst")
        or state.get("constraints_lpf")
        or state.get("cst_text")
        or state.get("lpf_text")
        or state.get("constraints_pcf")
        or state.get("pcf_text")
        or ""
    )
    source_path = state.get("cst_path") or state.get("lpf_path") or state.get("pcf_path")
    if not constraint_text and isinstance(source_path, str) and source_path and os.path.exists(source_path):
        with open(source_path, "r", encoding="utf-8", errors="ignore") as handle:
            constraint_text = handle.read()
    generated = False
    constrained_ports: list[str] = []
    if not constraint_text.strip():
        if fmt == "cst":
            constraint_text, constrained_ports = _starter_cst(str(top), frequency)
        elif fmt == "lpf":
            constraint_text, constrained_ports = _starter_lpf(str(top), frequency, board_key, rtl_ports)
        else:
            constraint_text, constrained_ports = _starter_pcf(str(top), frequency, board_key, rtl_ports)
        generated = True
    else:
        constrained_ports = _constrained_cst_ports(constraint_text) if fmt == "cst" else _constrained_ports_from_text(fmt, constraint_text)
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
        "cst_path": constraint_path if fmt == "cst" else None,
        "target_frequency_mhz": frequency,
        "board": board.get("board"),
        "note": "Generated demo constraints cover verified pins only. Custom interfaces must provide board-verified PCF, LPF, or CST assignments.",
    }
    if summary["unconstrained_ports"]:
        summary["status"] = "blocked"
        summary["error"] = (
            "FPGA constraints are incomplete. Provide a board-verified PCF, LPF, or CST, or select a board with "
            "a verified ChipLoop pin map for every top-level RTL port."
        )
        if generated:
            summary["routing_note"] = (
                "ChipLoop generated constraints only for ports with verified board pin mappings. No unconstrained "
                "fallback is allowed."
            )
    publish_json(state, agent, "constraints", "fpga_constraints_summary.json", summary)
    workflow_id = str(state.get("workflow_id") or "")
    if workflow_id:
        try:
            from utils.artifact_utils import save_text_artifact_and_record

            save_text_artifact_and_record(
                workflow_id,
                agent,
                "fpga/constraints",
                f"{top}.{fmt}",
                constraint_text,
            )
        except Exception:
            pass
    manifest_update(state, "constraints_pcf", constraint_path if fmt == "pcf" else None)
    manifest_update(state, "constraints_lpf", constraint_path if fmt == "lpf" else None)
    manifest_update(state, "constraints_path", constraint_path)
    manifest_update(state, "constraints_unconstrained_ports", summary["unconstrained_ports"])
    manifest_update(state, "target_frequency_mhz", frequency)
    manifest_update(state, "constraints", summary)
    manifest_update(state, "constraints_cst", constraint_path if fmt == "cst" else None)
    if summary["status"] == "blocked":
        state["status"] = "FPGA constraints incomplete."
        raise RuntimeError(f"{summary['error']} Unconstrained ports: {', '.join(summary['unconstrained_ports'])}")
    return state
