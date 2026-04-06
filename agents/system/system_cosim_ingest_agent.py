"""
System CoSim Ingest Agent
Production-oriented L2 validation ingest for System Software -> Firmware -> RTL co-simulation.

Key behaviors
- State-first resolution of software / firmware / RTL packages
- Supports firmware input from System Software Handoff Package Agent (Option A)
- Optional local file resolution from current workflow_dir
- Optional remote package restore via helper utilities if available
- Emits a normalized co-sim manifest for downstream agents
- Writes auditable artifacts through save_text_artifact_and_record when available
"""

import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

AGENT_NAME = "System CoSim Ingest Agent"
OUTPUT_SUBDIR = "system/validation/l2/ingest"

MANIFEST_JSON = "system_cosim_manifest.json"
SUMMARY_MD = "system_cosim_ingest_summary.md"
DEBUG_JSON = "system_cosim_ingest_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _record(workflow_id: str, filename: str, content: str):
    try:
        from utils.artifact_utils import save_text_artifact_and_record

        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _maybe_fetch_remote_json(workflow_id: str, candidates: List[str]) -> Dict[str, Any]:
    helper_names = [
        "utils.handoff_utils",
        "utils.supabase_utils",
        "utils.artifact_fetch_utils",
        "utils.remote_artifact_utils",
    ]
    for helper_name in helper_names:
        try:
            module = __import__(helper_name, fromlist=["dummy"])
        except Exception:
            continue
        for fn_name in [
            "load_json_artifact_from_workflow",
            "restore_json_artifact_from_workflow",
            "fetch_json_artifact_from_workflow",
            "get_json_artifact_from_workflow",
        ]:
            fn = getattr(module, fn_name, None)
            if not callable(fn):
                continue
            for candidate in candidates:
                try:
                    data = fn(workflow_id=workflow_id, artifact_path=candidate)
                    if isinstance(data, dict) and data:
                        return data
                except Exception:
                    pass
    return {}


def _find_first_local_json(root_dir: str, candidates: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    for rel in candidates:
        path = os.path.join(root_dir, rel)
        data = _safe_json(path)
        if data:
            return data, path
    return {}, None


def _resolve_package_from_state_or_local_or_remote(
    state: Dict[str, Any],
    state_keys: List[str],
    workflow_dir: str,
    workflow_id_hint: Optional[str],
    candidate_paths: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    debug = {
        "state_keys_checked": state_keys,
        "workflow_id_hint": workflow_id_hint,
        "candidate_paths": candidate_paths,
        "resolution": None,
        "resolved_path": None,
    }

    for key in state_keys:
        data = state.get(key)
        if isinstance(data, dict) and data:
            debug["resolution"] = f"state:{key}"
            return data, debug

    data, path = _find_first_local_json(workflow_dir, candidate_paths)
    if data:
        debug["resolution"] = "local"
        debug["resolved_path"] = path
        return data, debug

    if workflow_id_hint:
        data = _maybe_fetch_remote_json(str(workflow_id_hint), candidate_paths)
        if data:
            debug["resolution"] = "remote"
            return data, debug

    debug["resolution"] = "missing"
    return {}, debug


def _normalize_filelist(x: Any) -> List[str]:
    if isinstance(x, list):
        return [str(v).strip() for v in x if str(v).strip()]
    if isinstance(x, str):
        return [line.strip() for line in x.splitlines() if line.strip()]
    return []


def _derive_software_entry(pkg: Dict[str, Any]) -> Optional[str]:
    for key in [
        "software_entry",
        "entry",
        "app_entry",
        "main_binary",
        "default_app",
    ]:
        val = pkg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    apps = pkg.get("apps")
    if isinstance(apps, list) and apps:
        first = apps[0]
        if isinstance(first, dict):
            for key in ["entry", "name", "binary"]:
                val = first.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _derive_register_map(software_pkg: Dict[str, Any], firmware_pkg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        software_pkg.get("register_map"),
        firmware_pkg.get("register_map"),
        (software_pkg.get("artifacts") or {}).get("register_map"),
        (firmware_pkg.get("artifacts") or {}).get("register_map"),
        (firmware_pkg.get("manifest") or {}).get("register_map"),
    ]

    firmware_contract = firmware_pkg.get("firmware_contract") if isinstance(firmware_pkg.get("firmware_contract"), dict) else {}
    candidates.extend([
        firmware_contract.get("register_map_path"),
        firmware_contract.get("register_map"),
    ])

    for val in candidates:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _load_manifest_from_firmware_handoff(firmware_pkg: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    manifest = firmware_pkg.get("manifest")
    if isinstance(manifest, dict) and manifest:
        return manifest

    firmware_contract = firmware_pkg.get("firmware_contract") if isinstance(firmware_pkg.get("firmware_contract"), dict) else {}
    manifest_path = firmware_contract.get("firmware_manifest_path")
    if isinstance(manifest_path, str) and manifest_path.strip() and workflow_dir:
        return _safe_json(os.path.join(workflow_dir, manifest_path))

    return {}


def _derive_interrupts(firmware_pkg: Dict[str, Any], workflow_dir: str) -> List[str]:
    raw = firmware_pkg.get("interrupts")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    manifest = _load_manifest_from_firmware_handoff(firmware_pkg, workflow_dir)
    raw = manifest.get("interrupts")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    interrupt_map_path = ""
    firmware_contract = firmware_pkg.get("firmware_contract") if isinstance(firmware_pkg.get("firmware_contract"), dict) else {}
    if isinstance(firmware_contract.get("interrupt_mapping_path"), str):
        interrupt_map_path = firmware_contract.get("interrupt_mapping_path", "").strip()

    if interrupt_map_path and workflow_dir:
        interrupt_map = _safe_json(os.path.join(workflow_dir, interrupt_map_path))
        for key in ["interrupts", "interrupt_list", "irq_list"]:
            raw = interrupt_map.get(key)
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(interrupt_map, dict):
            keys = [str(k).strip() for k in interrupt_map.keys() if str(k).strip()]
            if keys:
                return keys

    return []


def _derive_dma_present(firmware_pkg: Dict[str, Any], rtl_pkg: Dict[str, Any], workflow_dir: str) -> Optional[bool]:
    for val in [
        firmware_pkg.get("dma_present"),
        (firmware_pkg.get("manifest") or {}).get("dma_present"),
        rtl_pkg.get("dma_present"),
        (rtl_pkg.get("integration_intent") or {}).get("dma_present"),
    ]:
        if isinstance(val, bool):
            return val

    manifest = _load_manifest_from_firmware_handoff(firmware_pkg, workflow_dir)
    hardware_features = manifest.get("hardware_features") if isinstance(manifest.get("hardware_features"), dict) else {}
    for key in ["has_dma", "dma_present"]:
        val = hardware_features.get(key)
        if isinstance(val, bool):
            return val

    firmware_contract = firmware_pkg.get("firmware_contract") if isinstance(firmware_pkg.get("firmware_contract"), dict) else {}
    dma_path = firmware_contract.get("dma_integration_path")
    if isinstance(dma_path, str) and dma_path.strip():
        return True

    return None


def _derive_firmware_elf(firmware_pkg: Dict[str, Any]) -> Optional[str]:
    for val in [
        firmware_pkg.get("firmware_elf"),
        firmware_pkg.get("elf"),
    ]:
        if isinstance(val, str) and val.strip():
            return val.strip()

    firmware_contract = firmware_pkg.get("firmware_contract") if isinstance(firmware_pkg.get("firmware_contract"), dict) else {}
    for key in ["elf_path", "elf"]:
        val = firmware_contract.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return None


def _derive_software_validation_status(state: Dict[str, Any], workflow_dir: str) -> Optional[str]:
    candidates = []
    for key in ["system_software_validation_summary", "software_validation_summary"]:
        data = state.get(key)
        if isinstance(data, dict) and data:
            candidates.append(data)

    for rel in [
        "system/validation/l1/system_software_validation_summary.json",
        "system/software_validation/system_software_validation_summary.json",
    ]:
        data = _safe_json(os.path.join(workflow_dir, rel))
        if data:
            candidates.append(data)

    for data in candidates:
        status = data.get("overall_status")
        if isinstance(status, str) and status.strip():
            return status.strip()
    return None


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or "")

    print(f"\n⚙️ Running {AGENT_NAME}")

    software_pkg, software_dbg = _resolve_package_from_state_or_local_or_remote(
        state=state,
        state_keys=[
            "system_software_validation_package",
            "system_software_package",
            "software_package",
        ],
        workflow_dir=workflow_dir,
        workflow_id_hint=state.get("system_software_workflow_id") or workflow_id,
        candidate_paths=[
            "system/software/package/system_software_package.json",
            "system/software_validation/package/system_software_validation_package.json",
            "software/package/system_software_package.json",
            "validation/l1/system_software_validation_package.json",
        ],
    )

    firmware_pkg, firmware_dbg = _resolve_package_from_state_or_local_or_remote(
        state=state,
        state_keys=[
            "system_firmware_handoff",
            "system_software_handoff",
            "system_firmware_package",
            "firmware_package",
        ],
        workflow_dir=workflow_dir,
        workflow_id_hint=state.get("system_firmware_workflow_id"),
        candidate_paths=[
            "system/software_handoff/system_software_handoff.json",
            "firmware/package/system_firmware_package.json",
            "firmware/package/firmware_package.json",
            "firmware/firmware_manifest.json",
            "system/firmware/package/system_firmware_package.json",
        ],
    )

    rtl_pkg, rtl_dbg = _resolve_package_from_state_or_local_or_remote(
        state=state,
        state_keys=["system_rtl_package", "rtl_package"],
        workflow_dir=workflow_dir,
        workflow_id_hint=state.get("system_rtl_workflow_id"),
        candidate_paths=[
            "system/package/system_rtl_package.json",
            "system/integration/system_rtl_package.json",
        ],
    )

    sim_filelist = _normalize_filelist(((rtl_pkg.get("filelists") or {}).get("sim")))
    phys_filelist = _normalize_filelist(((rtl_pkg.get("filelists") or {}).get("phys")))
    lib_filelist = _normalize_filelist(((rtl_pkg.get("filelists") or {}).get("libs")))

    software_entry = _derive_software_entry(software_pkg)
    firmware_elf = _derive_firmware_elf(firmware_pkg)
    register_map = _derive_register_map(software_pkg, firmware_pkg)
    interrupts = _derive_interrupts(firmware_pkg, workflow_dir)
    dma_present = _derive_dma_present(firmware_pkg, rtl_pkg, workflow_dir)

    compile_sim = ((rtl_pkg.get("compile") or {}).get("sim")) == "pass"
    rtl_ready_for_cosim = bool(rtl_pkg.get("ready_for_cosim"))

    top = rtl_pkg.get("top") or {}
    top_sim = top.get("sim") if isinstance(top, dict) else None

    software_validation_l1_status = _derive_software_validation_status(state, workflow_dir)
    software_l1_ready = (software_validation_l1_status == "ready") if software_validation_l1_status else None

    ready_for_l2_contract = bool(
        software_pkg and
        firmware_pkg and
        rtl_pkg and
        compile_sim and
        rtl_ready_for_cosim and
        sim_filelist
    )

    manifest: Dict[str, Any] = {
        "validation_scope": "full_stack",
        "generated_at": _now(),
        "agent": AGENT_NAME,
        "software": {
            "package_present": bool(software_pkg),
            "entry": software_entry,
            "package_type": software_pkg.get("package_type"),
            "l1_validation_status": software_validation_l1_status,
            "l1_ready": software_l1_ready,
        },
        "firmware": {
            "package_present": bool(firmware_pkg),
            "elf": firmware_elf,
            "register_map": register_map,
            "interrupts": interrupts,
            "dma_present": dma_present,
            "package_type": firmware_pkg.get("package_type"),
        },
        "rtl": {
            "package_present": bool(rtl_pkg),
            "top": top_sim,
            "compile_sim": "pass" if compile_sim else "fail",
            "ready_for_cosim": rtl_ready_for_cosim,
            "filelists": {
                "sim": sim_filelist,
                "phys": phys_filelist,
                "libs": lib_filelist,
            },
        },
        "readiness": {
            "software_present": bool(software_pkg),
            "firmware_present": bool(firmware_pkg),
            "rtl_present": bool(rtl_pkg),
            "rtl_sim_ready": bool(compile_sim and rtl_ready_for_cosim and sim_filelist),
            "ready_for_system_l2_contract": ready_for_l2_contract,
        },
    }

    debug = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "software_resolution": software_dbg,
        "firmware_resolution": firmware_dbg,
        "rtl_resolution": rtl_dbg,
        "manifest_checks": {
            "software_entry_found": bool(software_entry),
            "firmware_elf_found": bool(firmware_elf),
            "register_map_found": bool(register_map),
            "interrupt_count": len(interrupts),
            "dma_present_resolved": isinstance(dma_present, bool),
            "rtl_sim_file_count": len(sim_filelist),
            "software_l1_status_found": bool(software_validation_l1_status),
        },
    }

    summary = (
        "# CoSim Ingest Summary\n\n"
        f"- Software package present: {manifest['readiness']['software_present']}\n"
        f"- Software L1 status: {software_validation_l1_status or 'unknown'}\n"
        f"- Firmware package present: {manifest['readiness']['firmware_present']}\n"
        f"- Firmware ELF found: {bool(firmware_elf)}\n"
        f"- Register map found: {bool(register_map)}\n"
        f"- Interrupt count: {len(interrupts)}\n"
        f"- RTL package present: {manifest['readiness']['rtl_present']}\n"
        f"- RTL sim ready: {manifest['readiness']['rtl_sim_ready']}\n"
        f"- Ready for L2 contract: {manifest['readiness']['ready_for_system_l2_contract']}\n"
    )

    _record(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)

    state["system_cosim_manifest"] = manifest
    state["cosim_ingest_debug"] = debug
    state["status"] = "✅ CoSim ingest ready" if manifest["readiness"]["ready_for_system_l2_contract"] else "⚠️ CoSim ingest incomplete"
    return state
