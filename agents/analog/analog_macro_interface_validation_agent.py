import json
import os
from typing import Any, Dict, List

from agents.analog.analog_macro_interface_contract_agent import _parse_lef, _parse_lib, _parse_verilog_module, _read
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Macro Interface Validation Agent"


def _load_contract(state: dict, workflow_dir: str) -> Dict[str, Any]:
    value = state.get("analog_macro_interface_contract")
    if isinstance(value, dict) and value:
        return value
    for path in (
        state.get("analog_macro_interface_contract_path"),
        os.path.join(workflow_dir, "analog", "interface", "analog_macro_interface_contract.json"),
        os.path.join(workflow_dir, "system", "imported_macros", "analog_macro_interface_contract.json"),
    ):
        if isinstance(path, str) and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                    return loaded if isinstance(loaded, dict) else {}
            except Exception:
                pass
    return {}


def _contract_ports(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    ports = contract.get("ports")
    if not isinstance(ports, list):
        return {}
    return {str(p.get("name")): p for p in ports if isinstance(p, dict) and p.get("name")}


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "analog", "interface")
    os.makedirs(out_dir, exist_ok=True)

    contract = _load_contract(state, workflow_dir)
    issues: List[Dict[str, Any]] = []
    if not contract:
        issues.append({"severity": "error", "type": "contract_missing", "message": "analog_macro_interface_contract.json was not found."})

    c_ports = _contract_ports(contract)
    model_name, v_ports = _parse_verilog_module(_read(state.get("analog_model_path") or state.get("analog_model")), str(contract.get("macro_name") or ""))
    lef_name, lef_pins = _parse_lef(_read(state.get("analog_macro_lef")))
    lib_name, lib_pins = _parse_lib(_read(state.get("analog_macro_lib")))
    v_by_name = {p["name"]: p for p in v_ports}

    for source_name, observed_name in (("verilog_module", model_name), ("lef_macro", lef_name), ("lib_cell", lib_name)):
        expected = str(contract.get("macro_name") or "")
        if expected and observed_name and expected != observed_name:
            issues.append({"severity": "error", "type": f"{source_name}_name_mismatch", "expected": expected, "actual": observed_name})

    for name, expected in c_ports.items():
        if expected.get("verilog_direction") != "missing" and name not in v_by_name:
            issues.append({"severity": "error", "type": "verilog_pin_missing", "pin": name})
        if expected.get("lef_direction") != "missing" and name not in lef_pins:
            issues.append({"severity": "error", "type": "lef_pin_missing", "pin": name})
        if expected.get("lib_direction") != "missing" and name not in lib_pins:
            issues.append({"severity": "warning", "type": "lib_pin_missing", "pin": name})
        if name in v_by_name and expected.get("verilog_direction") not in {"missing", "unknown", v_by_name[name].get("verilog_direction")}:
            issues.append({"severity": "error", "type": "verilog_direction_mismatch", "pin": name, "expected": expected.get("verilog_direction"), "actual": v_by_name[name].get("verilog_direction")})
        if name in lef_pins and expected.get("lef_direction") not in {"missing", "UNKNOWN", lef_pins[name]}:
            issues.append({"severity": "error", "type": "lef_direction_mismatch", "pin": name, "expected": expected.get("lef_direction"), "actual": lef_pins[name]})
        if name in lib_pins and expected.get("lib_direction") not in {"missing", "unknown", lib_pins[name]}:
            issues.append({"severity": "warning", "type": "lib_direction_mismatch", "pin": name, "expected": expected.get("lib_direction"), "actual": lib_pins[name]})

    status = "fail" if any(i.get("severity") == "error" for i in issues) else "warning" if issues else "pass"
    report = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": status,
        "issues": issues,
        "contract_macro": contract.get("macro_name"),
        "observed": {
            "verilog_module": model_name,
            "lef_macro": lef_name,
            "lib_cell": lib_name,
            "verilog_pins": sorted(v_by_name),
            "lef_pins": sorted(lef_pins),
            "lib_pins": sorted(lib_pins),
        },
    }
    path = os.path.join(out_dir, "analog_macro_interface_validation.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/interface", "analog_macro_interface_validation.json", json.dumps(report, indent=2))
    state["analog_macro_interface_validation"] = report
    state["status"] = f"{AGENT_NAME}: {status}"
    if status == "fail" and str(state.get("analog_interface_fail_fast", "true")).lower() in {"1", "true", "yes"}:
        raise RuntimeError(f"Analog macro interface validation failed: {issues[:3]}")
    return state
