import json
import os
from datetime import datetime
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Physical Collateral Package Agent"


def _exists(path: Any) -> str:
    return os.path.abspath(path) if isinstance(path, str) and os.path.isfile(path) else ""


def _generate_mode(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "blackbox").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _analog_lvs_status(state: dict) -> str:
    gds_summary = state.get("analog_gds_generation") if isinstance(state.get("analog_gds_generation"), dict) else {}
    lvs = gds_summary.get("analog_lvs") if isinstance(gds_summary.get("analog_lvs"), dict) else {}
    signoff = state.get("analog_signoff") if isinstance(state.get("analog_signoff"), dict) else {}
    signoff_lvs = signoff.get("lvs") if isinstance(signoff.get("lvs"), dict) else {}
    return str(lvs.get("status") or signoff_lvs.get("status") or "").strip().lower()


def _analog_drc_status(state: dict) -> str:
    signoff = state.get("analog_signoff") if isinstance(state.get("analog_signoff"), dict) else {}
    signoff_drc = signoff.get("drc") if isinstance(signoff.get("drc"), dict) else {}
    gds_summary = state.get("analog_gds_generation") if isinstance(state.get("analog_gds_generation"), dict) else {}
    if signoff_drc.get("status"):
        return str(signoff_drc.get("status") or "").strip().lower()
    if gds_summary.get("status") == "generated":
        return "clean"
    return ""


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _lvs_source_spice(workflow_dir: str, module: str) -> str:
    candidates = [
        os.path.join(workflow_dir, "analog", "signoff", f"{module}_lvs_source.spice"),
    ]
    signoff_dir = os.path.join(workflow_dir, "analog", "signoff")
    if os.path.isdir(signoff_dir):
        candidates.extend(
            os.path.join(signoff_dir, name)
            for name in sorted(os.listdir(signoff_dir))
            if name.endswith("_lvs_source.spice")
        )
    return next((_exists(path) for path in candidates if _exists(path)), "")


def _state_lvs_source_spice(state: dict) -> str:
    gds_summary = state.get("analog_gds_generation") if isinstance(state.get("analog_gds_generation"), dict) else {}
    gds_lvs = gds_summary.get("analog_lvs") if isinstance(gds_summary.get("analog_lvs"), dict) else {}
    signoff = state.get("analog_signoff") if isinstance(state.get("analog_signoff"), dict) else {}
    signoff_lvs = signoff.get("lvs") if isinstance(signoff.get("lvs"), dict) else {}
    for value in (
        state.get("analog_lvs_source_spice"),
        signoff_lvs.get("source_spice"),
        gds_lvs.get("source_spice"),
    ):
        found = _exists(value)
        if found:
            return found
    return ""


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
    lvs_spice = _state_lvs_source_spice(state) or _lvs_source_spice(workflow_dir, module)
    generate_mode = _generate_mode(state)
    analog_lvs_status = _analog_lvs_status(state)
    analog_drc_status = _analog_drc_status(state)

    package_spice = lvs_spice or spice
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
        "spice": package_spice,
        "raw_spice": spice,
        "lvs_spice": lvs_spice,
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
        elif analog_drc_status and analog_drc_status not in {"clean", "ok", "pass"}:
            package["status"] = "failed"
            package["reason"] = "analog_drc_not_clean"
            package["analog_drc_status"] = analog_drc_status
        elif analog_lvs_status and analog_lvs_status not in {"clean", "ok", "pass"}:
            package["status"] = "failed"
            package["reason"] = "analog_lvs_not_clean"
            package["analog_lvs_status"] = analog_lvs_status
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
        if gds and package["status"] == "ready":
            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [gds]))
            digital.pop("macro_blackbox_mode", None)
        signoff_spice = lvs_spice or spice
        if signoff_spice:
            digital["macro_spice"] = list(dict.fromkeys((digital.get("macro_spice") or []) + [signoff_spice]))
            if lvs_spice:
                digital["macro_lvs_spice"] = list(dict.fromkeys((digital.get("macro_lvs_spice") or []) + [lvs_spice]))
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
        if package.get("missing"):
            raise RuntimeError(f"Analog physical collateral package failed: missing {', '.join(package['missing'])}")
        raise RuntimeError(f"Analog physical collateral package failed: {package.get('reason')}")
    return state
