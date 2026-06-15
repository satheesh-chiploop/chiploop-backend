import json
import os
from datetime import datetime
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Physical Collateral Package Agent"


def _exists(path: Any) -> str:
    return os.path.abspath(path) if isinstance(path, str) and os.path.exists(path) else ""


def _generate_mode(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "blackbox").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "analog", "physical_package")
    os.makedirs(out_dir, exist_ok=True)

    lef = _exists(state.get("analog_macro_lef"))
    lib = _exists(state.get("analog_macro_lib"))
    gds = _exists(state.get("analog_macro_gds"))
    spice = _exists(state.get("analog_spice_path") or state.get("analog_netlist_path"))
    mode = str(state.get("analog_physical_mode") or "blackbox").strip().lower()
    module = _module_name(state)
    generate_mode = _generate_mode(state)

    package: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "pdk": state.get("analog_pdk") or state.get("pdk") or "sky130A",
        "module": module,
        "lef": lef,
        "lib": lib,
        "gds": gds,
        "spice": spice,
        "blackbox_for_drc_lvs": bool((lef or lib) and not gds and not generate_mode),
        "blackbox_reason": "analog_macro_gds_missing" if (lef or lib) and not gds and not generate_mode else "",
        "gds_generation": state.get("analog_gds_generation") or {},
        "lef_extraction": state.get("analog_lef_extraction") or {},
        "liberty_characterization": state.get("analog_liberty_characterization") or {},
        "consistency": state.get("analog_collateral_consistency") or {},
        "spice_generation": state.get("analog_sky130_spice") or {},
    }

    if generate_mode:
        missing = []
        if not spice:
            missing.append("spice")
        if not gds:
            missing.append("gds")
        if not lef:
            missing.append("lef")
        if missing:
            package["status"] = "failed"
            package["reason"] = "analog_physical_collateral_missing"
            package["missing"] = missing
        else:
            package["status"] = "ready"
            package["reason"] = ""
    else:
        package["status"] = "ready" if (lef or lib or gds) else "blackbox_deferred"
        package["reason"] = package["blackbox_reason"]

    package_path = os.path.join(out_dir, "analog_physical_collateral_package.json")
    with open(package_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(package, indent=2))

    digital = state.setdefault("digital", {})
    if isinstance(digital, dict):
        if lef:
            digital["macro_lefs"] = list(dict.fromkeys((digital.get("macro_lefs") or []) + [lef]))
        if lib:
            digital["macro_libs"] = list(dict.fromkeys((digital.get("macro_libs") or []) + [lib]))
        if gds:
            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [gds]))
            digital.pop("macro_blackbox_mode", None)
        if spice:
            digital["macro_spice"] = list(dict.fromkeys((digital.get("macro_spice") or []) + [spice]))
        elif (lef or lib) and not generate_mode:
            digital["macro_blackbox_mode"] = "lef_lib_no_gds"
            digital["macro_blackboxes"] = [os.path.splitext(os.path.basename(lef))[0]] if lef else []
            state["physical_blackbox_macros"] = digital["macro_blackboxes"]

    state["analog_physical_collateral_package"] = package
    state["analog_physical_collateral_package_path"] = package_path
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "analog/physical_package",
        "analog_physical_collateral_package.json",
        json.dumps(package, indent=2),
    )
    state["status"] = f"{AGENT_NAME}: {package['status']}"
    if generate_mode and package["status"] != "ready":
        raise RuntimeError(f"Analog physical collateral package failed: missing {', '.join(package['missing'])}")
    return state
