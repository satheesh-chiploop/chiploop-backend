import json
import os
from typing import Any, Dict, List

from agents.analog.analog_macro_interface_contract_agent import _parse_lef, _parse_lib, _parse_verilog_module, _read, _role
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


def _normalize_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _existing_candidates(values: List[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        for path in _normalize_list(value):
            if os.path.exists(path):
                out.append(os.path.abspath(path))
    return list(dict.fromkeys(out))


def _walk_files(workflow_dir: str, suffixes: tuple[str, ...]) -> List[str]:
    if not workflow_dir or not os.path.isdir(workflow_dir):
        return []
    hits: List[str] = []
    for root, _, files in os.walk(workflow_dir):
        for name in files:
            if name.lower().endswith(suffixes):
                hits.append(os.path.abspath(os.path.join(root, name)))
    return sorted(hits)


def _first_macro_file(paths: List[str], macro_name: str, kind: str) -> str:
    for path in paths:
        text = _read(path)
        if kind == "verilog":
            name, _ports = _parse_verilog_module(text, macro_name)
        elif kind == "lef":
            name, _pins = _parse_lef(text)
        else:
            name, _pins = _parse_lib(text)
        if macro_name and name == macro_name:
            return path
    return paths[0] if paths else ""


def _resolve_inputs(state: dict, contract: Dict[str, Any], workflow_dir: str) -> Dict[str, str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    system = state.get("system") if isinstance(state.get("system"), dict) else {}
    sources = contract.get("sources") if isinstance(contract.get("sources"), dict) else {}
    macro_name = str(contract.get("macro_name") or state.get("analog_macro_module") or "").strip()

    model_candidates = _existing_candidates([
        state.get("analog_model_path"),
        state.get("analog_model"),
        sources.get("verilog_model"),
        digital.get("rtl_files") if isinstance(digital, dict) else [],
        system.get("rtl_files") if isinstance(system, dict) else [],
    ])
    if not model_candidates:
        model_candidates = _walk_files(workflow_dir, (".v", ".sv"))

    lef_candidates = _existing_candidates([
        state.get("analog_macro_lef"),
        sources.get("lef"),
        state.get("macro_lefs"),
        digital.get("macro_lefs") if isinstance(digital, dict) else [],
        system.get("macro_lefs") if isinstance(system, dict) else [],
    ])
    if not lef_candidates:
        lef_candidates = _walk_files(workflow_dir, (".lef",))

    lib_candidates = _existing_candidates([
        state.get("analog_macro_lib"),
        sources.get("lib"),
        state.get("macro_libs"),
        digital.get("macro_libs") if isinstance(digital, dict) else [],
        system.get("macro_libs") if isinstance(system, dict) else [],
    ])
    if not lib_candidates:
        lib_candidates = _walk_files(workflow_dir, (".lib", ".db"))

    model_path = _first_macro_file(model_candidates, macro_name, "verilog")
    lef_path = _first_macro_file(lef_candidates, macro_name, "lef")
    lib_path = _first_macro_file(lib_candidates, macro_name, "lib")

    if model_path:
        state["analog_model_path"] = model_path
    if lef_path:
        state["analog_macro_lef"] = lef_path
    if lib_path:
        state["analog_macro_lib"] = lib_path

    return {"model": model_path, "lef": lef_path, "lib": lib_path}


def _pin_base(name: str) -> str:
    return str(name or "").split("[", 1)[0]


def _has_pin(name: str, observed: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    if name in observed:
        return True
    base = _pin_base(name)
    if "[" in name and base in observed:
        return True
    if "[" not in name and any(_pin_base(pin) == name for pin in observed):
        return True

    role = str(expected.get("role") or _role(name))
    if role in {"power", "ground"}:
        return any(_role(pin) == role for pin in observed)
    return False


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
    resolved = _resolve_inputs(state, contract, workflow_dir)
    model_name, v_ports = _parse_verilog_module(_read(resolved.get("model")), str(contract.get("macro_name") or ""))
    lef_name, lef_pins = _parse_lef(_read(resolved.get("lef")))
    lib_name, lib_pins = _parse_lib(_read(resolved.get("lib")))
    v_by_name = {p["name"]: p for p in v_ports}

    for source_name, observed_name in (("verilog_module", model_name), ("lef_macro", lef_name), ("lib_cell", lib_name)):
        expected = str(contract.get("macro_name") or "")
        if expected and observed_name and expected != observed_name:
            issues.append({"severity": "error", "type": f"{source_name}_name_mismatch", "expected": expected, "actual": observed_name})

    for name, expected in c_ports.items():
        if expected.get("verilog_direction") != "missing" and not _has_pin(name, v_by_name, expected):
            issues.append({"severity": "error", "type": "verilog_pin_missing", "pin": name})
        if expected.get("lef_direction") != "missing" and not _has_pin(name, lef_pins, expected):
            issues.append({"severity": "error", "type": "lef_pin_missing", "pin": name})
        if expected.get("lib_direction") != "missing" and not _has_pin(name, lib_pins, expected):
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
        "resolved_sources": resolved,
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
