import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Macro Interface Contract Agent"


def _read(path: Any) -> str:
    if not isinstance(path, str) or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _parse_verilog_module(text: str, preferred: str = "") -> tuple[str, List[Dict[str, Any]]]:
    modules = list(re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*?)\)\s*;", text or "", flags=re.DOTALL))
    if not modules:
        return "", []
    selected = next((m for m in modules if preferred and m.group(1) == preferred), modules[0])
    module_name = selected.group(1)
    header = selected.group(2)
    body_start = selected.end()
    body_end = text.find("endmodule", body_start)
    body = text[body_start:body_end if body_end >= 0 else len(text)]
    port_order = [p.strip().split()[-1].strip(" ,") for p in header.replace("\n", " ").split(",") if p.strip()]
    decl_text = header + "\n" + body
    decls: Dict[str, Dict[str, Any]] = {}
    for match in re.finditer(r"\b(input|output|inout)\b\s+(?:wire|logic|reg\s+)?(\[[^\]]+\])?\s*([^;,\n)]+(?:\s*,\s*[^;,\n)]+)*)", decl_text):
        direction = match.group(1)
        width = (match.group(2) or "1").strip()
        names = [x.strip().split()[-1] for x in match.group(3).split(",")]
        for name in names:
            if name:
                decls[name] = {"name": name, "verilog_direction": direction, "width": width}
    ports = []
    for name in port_order or sorted(decls):
        clean = re.sub(r"[^A-Za-z0-9_$]", "", name)
        if clean:
            ports.append(decls.get(clean, {"name": clean, "verilog_direction": "unknown", "width": "unknown"}))
    return module_name, ports


def _parse_lef(text: str) -> tuple[str, Dict[str, str]]:
    macro = ""
    m = re.search(r"^\s*MACRO\s+(\S+)", text or "", flags=re.IGNORECASE | re.MULTILINE)
    if m:
        macro = m.group(1)
    pins: Dict[str, str] = {}
    for pin_match in re.finditer(r"^\s*PIN\s+(\S+)(.*?)(?=^\s*PIN\s+|\s*END\s+\1\b)", text or "", flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
        name = pin_match.group(1)
        block = pin_match.group(2)
        direction = "UNKNOWN"
        d = re.search(r"\bDIRECTION\s+(\S+)", block, flags=re.IGNORECASE)
        if d:
            direction = d.group(1).upper().rstrip(";")
        pins[name] = direction
    return macro, pins


def _parse_lib(text: str) -> tuple[str, Dict[str, str]]:
    cell = ""
    m = re.search(r"\bcell\s*\(\s*([^) ]+)\s*\)", text or "", flags=re.IGNORECASE)
    if m:
        cell = m.group(1)
    pins: Dict[str, str] = {}
    for pin_match in re.finditer(r"\bpin\s*\(\s*([^) ]+)\s*\)\s*\{(.*?)\}", text or "", flags=re.IGNORECASE | re.DOTALL):
        name = pin_match.group(1)
        block = pin_match.group(2)
        direction = "unknown"
        d = re.search(r"\bdirection\s*:\s*([A-Za-z_]+)", block, flags=re.IGNORECASE)
        if d:
            direction = d.group(1).lower()
        pins[name] = direction
    return cell, pins


def _role(name: str) -> str:
    low = name.lower()
    if low in {"vdd", "avdd", "dvdd", "vpwr"} or "vdd" in low or "pwr" in low:
        return "power"
    if low in {"vss", "avss", "dvss", "gnd", "vgnd"} or "vss" in low or "gnd" in low:
        return "ground"
    if "clk" in low:
        return "clock"
    if "rst" in low or "reset" in low:
        return "reset"
    return "signal"


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "analog", "interface")
    os.makedirs(out_dir, exist_ok=True)

    module_hint = str(state.get("analog_macro_module") or "").strip()
    model_text = _read(state.get("analog_model_path") or state.get("analog_model"))
    lef_text = _read(state.get("analog_macro_lef"))
    lib_text = _read(state.get("analog_macro_lib"))
    module_name, v_ports = _parse_verilog_module(model_text, module_hint)
    lef_macro, lef_pins = _parse_lef(lef_text)
    lib_cell, lib_pins = _parse_lib(lib_text)
    macro_name = module_name or lef_macro or lib_cell or module_hint or "analog_macro"
    names = sorted({p["name"] for p in v_ports} | set(lef_pins) | set(lib_pins))
    v_by_name = {p["name"]: p for p in v_ports}
    ports = []
    for name in names:
        ports.append({
            "name": name,
            "role": _role(name),
            "width": v_by_name.get(name, {}).get("width", "unknown"),
            "verilog_direction": v_by_name.get(name, {}).get("verilog_direction", "missing"),
            "lef_direction": lef_pins.get(name, "missing"),
            "lib_direction": lib_pins.get(name, "missing"),
        })

    contract = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "macro_name": macro_name,
        "sources": {
            "verilog_model": state.get("analog_model_path") or state.get("analog_model"),
            "lef": state.get("analog_macro_lef"),
            "lib": state.get("analog_macro_lib"),
        },
        "source_names": {
            "verilog_module": module_name,
            "lef_macro": lef_macro,
            "lib_cell": lib_cell,
        },
        "ports": ports,
        "power_pins": [p["name"] for p in ports if p["role"] == "power"],
        "ground_pins": [p["name"] for p in ports if p["role"] == "ground"],
    }
    path = os.path.join(out_dir, "analog_macro_interface_contract.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(contract, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/interface", "analog_macro_interface_contract.json", json.dumps(contract, indent=2))
    state["analog_macro_interface_contract"] = contract
    state["analog_macro_interface_contract_path"] = path
    state["status"] = f"{AGENT_NAME}: generated"
    return state
