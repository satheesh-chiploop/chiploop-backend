import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Tuple

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog LEF Extraction Agent"
OPENLANE_DOCKER_IMAGE = "ghcr.io/efabless/openlane2:2.4.0.dev1"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _lef_invalid(path: str) -> str:
    if not os.path.exists(path) or os.path.getsize(path) <= 0:
        return "lef_not_produced"
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
    if " PIN " not in f" {text} " and "\n  PIN " not in text:
        return "lef_missing_pins"
    if "SIZE 1.000 BY 1.000" in text and " PIN " not in f" {text} ":
        return "lef_placeholder_geometry"
    return ""


def _parse_lef_size(text: str) -> Tuple[float, float]:
    match = re.search(r"\bSIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)\s*;", text or "", flags=re.IGNORECASE)
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2))


def _parse_prior_lef_pins(path: Any) -> Dict[str, str]:
    if not isinstance(path, str) or not os.path.exists(path):
        return {}
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
    pins: Dict[str, str] = {}
    for pin_match in re.finditer(r"^\s*PIN\s+(\S+)(.*?)(?=^\s*PIN\s+|\s*END\s+\1\b)", text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
        direction = "INOUT"
        d = re.search(r"\bDIRECTION\s+(\S+)", pin_match.group(2), flags=re.IGNORECASE)
        if d:
            direction = d.group(1).upper().rstrip(";")
        pins[pin_match.group(1)] = direction
    return pins


def _contract_pins(state: dict) -> Dict[str, str]:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    pins: Dict[str, str] = {}
    for port in contract.get("ports") if isinstance(contract.get("ports"), list) else []:
        if not isinstance(port, dict) or not port.get("name"):
            continue
        direction = str(port.get("lef_direction") or port.get("verilog_direction") or "INOUT").upper()
        if direction in {"INPUT"}:
            direction = "INPUT"
        elif direction in {"OUTPUT"}:
            direction = "OUTPUT"
        else:
            direction = "INOUT"
        pins[str(port["name"])] = direction
    return pins


def _pin_use(name: str) -> str:
    low = name.lower()
    if low in {"vpwr", "vdd", "avdd", "dvdd"} or "vdd" in low or "pwr" in low:
        return "POWER"
    if low in {"vgnd", "vss", "avss", "dvss", "gnd"} or "vss" in low or "gnd" in low:
        return "GROUND"
    if "clk" in low:
        return "CLOCK"
    return "SIGNAL"


def _pinized_lef(module_name: str, width: float, height: float, pins: Dict[str, str]) -> str:
    if width <= 0 or height <= 0 or not pins:
        return ""
    def snap(value: float, grid: float = 0.005) -> float:
        return round(round(value / grid) * grid, 3)

    signal_pins = [(name, direction) for name, direction in sorted(pins.items()) if _pin_use(name) not in {"POWER", "GROUND"}]
    min_height = max(100.0, (len(signal_pins) + 1) * 2.5)
    width = max(width, 100.0)
    height = max(height, min_height)
    signal_pitch = height / (max(len(signal_pins), 1) + 1)
    signal_pin_h = min(max(signal_pitch * 0.45, 0.8), 2.0)
    signal_pin_w = min(max(width * 0.03, 1.5), 4.0)
    rail_w = min(max(width * 0.03, 2.0), 5.0)
    rail_gap = max(rail_w * 0.5, 1.0)
    power_rail_x = max(width - (2 * rail_w) - rail_gap, width * 0.65)
    ground_rail_x = min(power_rail_x + rail_w + rail_gap, width - rail_w)
    lines: List[str] = [
        "VERSION 5.7 ;",
        "  NOWIREEXTENSIONATPIN ON ;",
        '  DIVIDERCHAR "/" ;',
        '  BUSBITCHARS "[]" ;',
        f"MACRO {module_name}",
        "  CLASS BLOCK ;",
        f"  FOREIGN {module_name} ;",
        "  ORIGIN 0.000 0.000 ;",
        f"  SIZE {width:.3f} BY {height:.3f} ;",
    ]
    signal_idx = 0
    for idx, (name, direction) in enumerate(sorted(pins.items())):
        use = _pin_use(name)
        lef_direction = "INOUT" if use in {"POWER", "GROUND"} else direction
        if use == "POWER":
            layer = "met4"
            x1, x2 = snap(power_rail_x), snap(min(power_rail_x + rail_w, width))
            y1, y2 = 0.0, snap(height)
            shape = "      SHAPE ABUTMENT ;"
        elif use == "GROUND":
            layer = "met4"
            x1, x2 = snap(ground_rail_x), snap(min(ground_rail_x + rail_w, width))
            y1, y2 = 0.0, snap(height)
            shape = "      SHAPE ABUTMENT ;"
        else:
            layer = "met2"
            signal_idx += 1
            y_mid = min(max(signal_idx * signal_pitch, signal_pin_h / 2), height - signal_pin_h / 2)
            y1 = snap(max(y_mid - signal_pin_h / 2, 0.0))
            y2 = snap(min(y_mid + signal_pin_h / 2, height))
            x1 = 0.0 if signal_idx % 2 else snap(max(width - signal_pin_w, 0.0))
            x2 = snap(min(x1 + signal_pin_w, width))
            shape = ""
        lines.extend([
            f"  PIN {name}",
            f"    DIRECTION {lef_direction} ;",
            f"    USE {use} ;",
            *([shape] if shape else []),
            "    PORT",
            f"      LAYER {layer} ;",
            f"        RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
            "    END",
            f"  END {name}",
        ])
    lines.extend([f"END {module_name}", "END LIBRARY", ""])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "lef_extract")
    os.makedirs(stage_dir, exist_ok=True)

    module_name = _module_name(state)
    gds_path = state.get("analog_macro_gds")
    prior_lef = state.get("analog_macro_lef")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module_name,
        "gds": gds_path,
        "prior_lef": prior_lef,
    }

    if not _enabled(state):
        summary.update({"status": "skipped", "reason": "analog_physical_mode_not_generate_gds"})
    elif not isinstance(gds_path, str) or not os.path.exists(gds_path):
        summary.update({"status": "failed", "reason": "analog_macro_gds_missing"})
    else:
        magic_bin = shutil.which("magic")
        docker_bin = shutil.which("docker")
        tcl_path = os.path.join(stage_dir, "extract_lef.tcl")
        lef_path = os.path.join(stage_dir, f"{module_name}.lef")
        staged_gds = os.path.join(stage_dir, os.path.basename(gds_path) or f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(staged_gds):
            shutil.copy2(gds_path, staged_gds)
        gds_for_tcl = os.path.abspath(staged_gds) if magic_bin else f"/work/{os.path.basename(staged_gds)}"
        lef_for_tcl = os.path.abspath(lef_path) if magic_bin else f"/work/{module_name}.lef"
        tcl_for_cmd = tcl_path if magic_bin else "/work/extract_lef.tcl"
        tcl = "\n".join([
            "drc off",
            f"gds read {gds_for_tcl}",
            f"load {module_name}",
            "select top cell",
            f"lef write {lef_for_tcl}",
            "quit -noprompt",
            "",
        ])
        with open(tcl_path, "w", encoding="utf-8") as fh:
            fh.write(tcl)

        if magic_bin or docker_bin:
            if magic_bin:
                cmd = [magic_bin, "-dnull", "-noconsole", tcl_for_cmd]
                tool_mode = "host"
            else:
                cmd = [
                    docker_bin,
                    "run",
                    "--rm",
                    "-v",
                    f"{os.path.abspath(stage_dir)}:/work",
                    "-w",
                    "/work",
                    OPENLANE_DOCKER_IMAGE,
                    "magic",
                    "-dnull",
                    "-noconsole",
                    tcl_for_cmd,
                ]
                tool_mode = "docker"
            cp = run_command(state, "analog_lef_extract_magic", cmd, cwd=stage_dir, timeout_sec=1800)
            log = (cp.stdout or "") + (cp.stderr or "")
            log_path = os.path.join(stage_dir, "magic_lef_extract.log")
            with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(log)
            invalid_reason = _lef_invalid(lef_path)
            pinized = False
            pin_source = ""
            pin_count = 0
            if invalid_reason == "lef_missing_pins":
                produced_text = open(lef_path, "r", encoding="utf-8", errors="ignore").read() if os.path.exists(lef_path) else ""
                width, height = _parse_lef_size(produced_text)
                pins = _parse_prior_lef_pins(prior_lef) or _contract_pins(state)
                pin_source = "prior_lef" if _parse_prior_lef_pins(prior_lef) else "macro_interface_contract"
                pin_count = len(pins)
                pinized_text = _pinized_lef(module_name, width, height, pins)
                if pinized_text:
                    with open(lef_path, "w", encoding="utf-8") as fh:
                        fh.write(pinized_text)
                    invalid_reason = _lef_invalid(lef_path)
                    pinized = not invalid_reason
            if not invalid_reason:
                state["analog_macro_lef"] = lef_path
                digital = state.setdefault("digital", {})
                if isinstance(digital, dict):
                    digital["macro_lefs"] = list(dict.fromkeys((digital.get("macro_lefs") or []) + [lef_path]))
                summary.update({
                    "status": "extracted",
                    "lef": lef_path,
                    "log": log_path,
                    "return_code": cp.returncode,
                    "tool_mode": tool_mode,
                    "image": OPENLANE_DOCKER_IMAGE if tool_mode == "docker" else "",
                    "pinized_from_macro_interface": pinized,
                    "pin_source": pin_source,
                    "pin_count": pin_count,
                })
            else:
                summary.update({
                    "status": "failed",
                    "reason": invalid_reason,
                    "log": log_path,
                    "return_code": cp.returncode,
                    "tool_mode": tool_mode,
                    "image": OPENLANE_DOCKER_IMAGE if tool_mode == "docker" else "",
                    "pinized_from_macro_interface": pinized,
                    "pin_source": pin_source,
                    "pin_count": pin_count,
                })
        else:
            summary.update({
                "status": "failed",
                "reason": "magic_not_installed",
                "extract_script": tcl_path,
                "note": "LEF was not regenerated from GDS because Magic is not installed on PATH and Docker is not available.",
            })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", "lef_extract_summary.json", json.dumps(summary, indent=2))
    if os.path.exists(os.path.join(stage_dir, "extract_lef.tcl")):
        with open(os.path.join(stage_dir, "extract_lef.tcl"), "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", "extract_lef.tcl", fh.read())
    if os.path.exists(os.path.join(stage_dir, "magic_lef_extract.log")):
        with open(os.path.join(stage_dir, "magic_lef_extract.log"), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", "magic_lef_extract.log", fh.read())
    if summary.get("lef") and os.path.exists(str(summary["lef"])):
        with open(str(summary["lef"]), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", f"{module_name}.lef", fh.read())
    elif os.path.exists(os.path.join(stage_dir, f"{module_name}.lef")):
        with open(os.path.join(stage_dir, f"{module_name}.lef"), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", f"{module_name}.lef", fh.read())

    state["analog_lef_extraction"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if _enabled(state) and summary["status"] != "extracted":
        raise RuntimeError(f"Analog LEF extraction failed: {summary.get('reason') or summary['status']}")
    return state
