import json
import os
from datetime import datetime
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Physical Collateral Package Agent"


def _exists(path: Any) -> str:
    return os.path.abspath(path) if isinstance(path, str) and os.path.exists(path) else ""


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

    package: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "pdk": state.get("analog_pdk") or state.get("pdk") or "sky130A",
        "module": state.get("analog_macro_module") or "analog_macro",
        "lef": lef,
        "lib": lib,
        "gds": gds,
        "spice": spice,
        "blackbox_for_drc_lvs": bool((lef or lib) and not gds),
        "blackbox_reason": "analog_macro_gds_missing" if (lef or lib) and not gds else "",
        "gds_generation": state.get("analog_gds_generation") or {},
        "lef_extraction": state.get("analog_lef_extraction") or {},
        "liberty_characterization": state.get("analog_liberty_characterization") or {},
        "consistency": state.get("analog_collateral_consistency") or {},
        "spice_generation": state.get("analog_sky130_spice") or {},
    }

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
        elif lef or lib:
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
    state["status"] = f"{AGENT_NAME}: {'gds_ready' if gds else 'blackbox_deferred'}"
    return state
