import json
import os
import re
import shlex
from datetime import datetime
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Collateral Consistency Agent"


def _read(path: Any) -> str:
    if not isinstance(path, str) or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _subckt(text: str) -> tuple[str, List[str]]:
    m = re.search(r"^\s*\.subckt\s+(\S+)\s+(.+)$", text or "", flags=re.IGNORECASE | re.MULTILINE)
    if not m:
        return "", []
    try:
        pins = shlex.split(m.group(2))
    except ValueError:
        pins = m.group(2).split()
    return m.group(1), _normalize_pins(pins)


def _lef_macro(text: str) -> tuple[str, List[str]]:
    macro = ""
    pins: List[str] = []
    m = re.search(r"^\s*MACRO\s+(\S+)", text or "", flags=re.IGNORECASE | re.MULTILINE)
    if m:
        macro = m.group(1)
    pins = re.findall(r"^\s*PIN\s+(\S+)", text or "", flags=re.IGNORECASE | re.MULTILINE)
    return macro, _normalize_pins(pins)


def _lib_cell(text: str) -> tuple[str, List[str]]:
    cell = ""
    pins: List[str] = []
    m = re.search(r"\bcell\s*\(\s*([^) ]+)\s*\)", text or "", flags=re.IGNORECASE)
    if m:
        cell = m.group(1)
    pins = re.findall(r"\bpin\s*\(\s*([^) ]+)\s*\)", text or "", flags=re.IGNORECASE)
    return cell, _normalize_pins(pins)


def _generate_mode(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _normalize_pin(name: str) -> str:
    return str(name or "").strip().strip("\"'").strip()


def _normalize_pins(pins: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for pin in pins:
        normalized = _normalize_pin(pin)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _pin_base(name: str) -> str:
    return _normalize_pin(name).split("[", 1)[0]


def _pin_covered(pin: str, observed: List[str]) -> bool:
    pin = _normalize_pin(pin)
    observed = _normalize_pins(observed)
    if pin in observed:
        return True
    base = _pin_base(pin)
    if "[" not in pin and any(_pin_base(obs) == pin and "[" in obs for obs in observed):
        return True
    if "[" in pin and base in observed:
        return True
    return False


def _missing_pins(expected: List[str], observed: List[str]) -> List[str]:
    return sorted(pin for pin in set(expected) if not _pin_covered(pin, observed))


def _lib_is_generated(state: dict) -> bool:
    char = state.get("analog_liberty_characterization")
    if not isinstance(char, dict):
        return False
    return bool(char.get("generated_liberty") or char.get("status") == "generated")


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "analog", "consistency")
    os.makedirs(out_dir, exist_ok=True)

    module = _module_name(state)
    spice = _read(state.get("analog_spice_path") or state.get("analog_netlist_path"))
    lef = _read(state.get("analog_macro_lef"))
    lib = _read(state.get("analog_macro_lib"))
    gds_present = isinstance(state.get("analog_macro_gds"), str) and os.path.exists(state.get("analog_macro_gds"))

    subckt_name, spice_pins = _subckt(spice)
    lef_name, lef_pins = _lef_macro(lef)
    lib_name, lib_pins = _lib_cell(lib)
    issues: List[str] = []
    warnings: List[str] = []

    if gds_present and not lef:
        issues.append("gds_present_but_lef_missing")
    if spice and lef and subckt_name and lef_name and subckt_name != lef_name:
        issues.append("spice_subckt_name_differs_from_lef_macro")
    if lib and lef and lib_name and lef_name and lib_name != lef_name:
        issues.append("lib_cell_name_differs_from_lef_macro")
    if spice_pins and lef_pins:
        missing_in_lef = _missing_pins(spice_pins, lef_pins)
        if missing_in_lef:
            issues.append(f"spice_pins_missing_in_lef:{','.join(missing_in_lef)}")
    if lib_pins and lef_pins:
        missing_in_lib = _missing_pins(lef_pins, lib_pins)
        if missing_in_lib:
            target = issues if _lib_is_generated(state) else warnings
            target.append(f"lef_pins_missing_in_lib:{','.join(missing_in_lib)}")

    status = "issues" if issues else "warnings" if warnings else "pass"

    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module,
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "gds_present": gds_present,
        "spice": {"subckt": subckt_name, "pins": spice_pins},
        "lef": {"macro": lef_name, "pins": lef_pins},
        "lib": {"cell": lib_name, "pins": lib_pins},
    }

    path = os.path.join(out_dir, "analog_collateral_consistency.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/consistency", "analog_collateral_consistency.json", json.dumps(summary, indent=2))

    state["analog_collateral_consistency"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if _generate_mode(state) and issues:
        raise RuntimeError(f"Analog collateral consistency failed: {', '.join(issues)}")
    return state
