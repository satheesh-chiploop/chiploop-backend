"""
System CoSim Ingest Agent
Production-worthy L2 validation ingest for System Software -> Firmware -> RTL co-simulation.

Key behaviors
- State-first resolution of software / firmware / RTL packages
- Supports firmware input from System Software Handoff Package Agent (Option A)
- Local workflow_dir resolution when present
- Explicit Supabase artifact restore using workflow ids and storage prefixes
- Emits a normalized co-sim manifest for downstream agents
- Writes auditable artifacts through save_text_artifact_and_record when available
"""

import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

AGENT_NAME = "System CoSim Ingest Agent"
OUTPUT_SUBDIR = "system/validation/l2/ingest"
ARTIFACT_BUCKET = "artifacts"

MANIFEST_JSON = "system_cosim_manifest.json"
SUMMARY_MD = "system_cosim_ingest_summary.md"
DEBUG_JSON = "system_cosim_ingest_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


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


def _get_supabase(state: Dict[str, Any]):
    client = state.get("supabase_client")
    if client is None:
        raise RuntimeError("supabase_client missing from state")
    return client


def _workflow_row(supabase, workflow_id: str) -> Dict[str, Any]:
    if not workflow_id:
        return {}
    try:
        resp = (
            supabase.table("workflows")
            .select("id,user_id,name,loop_type,artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
        )
        return resp.data or {}
    except Exception:
        return {}


def _workflow_storage_prefixes(state: Dict[str, Any], source_workflow_id: str, workflow_row: Dict[str, Any]) -> List[str]:
    prefixes: List[str] = []

    for key in ("artifact_prefix", "source_artifact_prefix"):
        v = state.get(key)
        if isinstance(v, str) and v.strip():
            prefixes.append(_norm_path(v).rstrip("/") + "/")

    prefixes.append(f"backend/workflows/{source_workflow_id}/")

    user_id = (workflow_row or {}).get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        prefixes.append(f"artifacts/{user_id}/{source_workflow_id}/")
        prefixes.append(f"{user_id}/{source_workflow_id}/")

    out: List[str] = []
    seen = set()
    for p in prefixes:
        p = _norm_path(p)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_supabase_artifact_path(supabase, prefixes: List[str], rel_or_abs: str) -> str:
    target = _norm_path(rel_or_abs)
    if not target:
        return ""

    candidates: List[str] = []
    if target.startswith("backend/workflows/") or target.startswith("artifacts/"):
        candidates.append(target)
    else:
        for prefix in prefixes:
            candidates.append(f"{prefix.rstrip('/')}/{target}")
        candidates.append(target)

    ordered: List[str] = []
    seen = set()
    for c in candidates:
        c = _norm_path(c)
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    for candidate in ordered:
        try:
            supabase.storage.from_(ARTIFACT_BUCKET).download(candidate)
            return candidate
        except Exception:
            pass
    return ""


def _load_json_from_supabase(supabase, prefixes: List[str], rel_or_abs: str) -> Tuple[Dict[str, Any], str]:
    resolved = _resolve_supabase_artifact_path(supabase, prefixes, rel_or_abs)
    if not resolved:
        return {}, ""
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(resolved)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        obj = json.loads(text)
        return (obj if isinstance(obj, dict) else {}), resolved
    except Exception:
        return {}, ""


def _find_first_local_json(root_dir: str, candidates: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    for rel in candidates:
        path = os.path.join(root_dir, rel)
        data = _safe_json(path)
        if data:
            return data, path
    return {}, None


def _resolve_package_from_state_local_or_supabase(
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
        "storage_prefixes": [],
        "workflow_row_found": False,
    }

    for key in state_keys:
        data = state.get(key)
        if isinstance(data, dict) and data:
            debug["resolution"] = f"state:{key}"
            debug["resolved_path"] = f"state:{key}"
            return data, debug

    data, path = _find_first_local_json(workflow_dir, candidate_paths)
    if data:
        debug["resolution"] = "local"
        debug["resolved_path"] = path
        return data, debug

    if workflow_id_hint:
        try:
            supabase = _get_supabase(state)
            wf_row = _workflow_row(supabase, str(workflow_id_hint))
            prefixes = _workflow_storage_prefixes(state, str(workflow_id_hint), wf_row)
            debug["storage_prefixes"] = prefixes
            debug["workflow_row_found"] = bool(wf_row)
            for candidate in candidate_paths:
                data, resolved = _load_json_from_supabase(supabase, prefixes, candidate)
                if data:
                    debug["resolution"] = "supabase"
                    debug["resolved_path"] = resolved
                    return data, debug
        except Exception as exc:
            debug["supabase_error"] = str(exc)

    debug["resolution"] = "missing"
    return {}, debug


def _normalize_filelist(x: Any) -> List[str]:
    if isinstance(x, list):
        return [str(v).strip() for v in x if str(v).strip()]
    if isinstance(x, str):
        return [line.strip() for line in x.splitlines() if line.strip()]
    return []


def _derive_software_entry(pkg: Dict[str, Any]) -> Optional[str]:
    for key in ["software_entry", "entry", "app_entry", "main_binary", "default_app"]:
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


def _derive_firmware_contract(firmware_pkg: Dict[str, Any]) -> Dict[str, Any]:
    fc = firmware_pkg.get("firmware_contract")
    return fc if isinstance(fc, dict) else {}


def _load_manifest_from_firmware_handoff(firmware_pkg: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    manifest = firmware_pkg.get("manifest")
    if isinstance(manifest, dict) and manifest:
        return manifest

    firmware_contract = _derive_firmware_contract(firmware_pkg)
    manifest_path = firmware_contract.get("firmware_manifest_path")
    if isinstance(manifest_path, str) and manifest_path.strip() and workflow_dir:
        return _safe_json(os.path.join(workflow_dir, manifest_path))
    return {}


def _derive_register_map(software_pkg: Dict[str, Any], firmware_pkg: Dict[str, Any]) -> Optional[str]:
    firmware_contract = _derive_firmware_contract(firmware_pkg)
    candidates = [
        software_pkg.get("register_map"),
        firmware_pkg.get("register_map"),
        (software_pkg.get("artifacts") or {}).get("register_map"),
        (firmware_pkg.get("artifacts") or {}).get("register_map"),
        (firmware_pkg.get("manifest") or {}).get("register_map"),
        firmware_contract.get("register_map_path"),
        firmware_contract.get("register_map"),
    ]
    for val in candidates:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _derive_interrupts(firmware_pkg: Dict[str, Any], workflow_dir: str) -> List[str]:
    raw = firmware_pkg.get("interrupts")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    manifest = _load_manifest_from_firmware_handoff(firmware_pkg, workflow_dir)
    raw = manifest.get("interrupts")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    firmware_contract = _derive_firmware_contract(firmware_pkg)
    interrupt_map_path = firmware_contract.get("interrupt_mapping_path", "")
    if isinstance(interrupt_map_path, str) and interrupt_map_path.strip() and workflow_dir:
        interrupt_map = _safe_json(os.path.join(workflow_dir, interrupt_map_path.strip()))
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

    firmware_contract = _derive_firmware_contract(firmware_pkg)
    dma_path = firmware_contract.get("dma_integration_path")
    if isinstance(dma_path, str) and dma_path.strip():
        return True

    return None


def _derive_firmware_elf(firmware_pkg: Dict[str, Any]) -> Optional[str]:
    firmware_contract = _derive_firmware_contract(firmware_pkg)
    for val in [
        firmware_pkg.get("firmware_elf"),
        firmware_pkg.get("elf"),
        firmware_contract.get("elf_path"),
        firmware_contract.get("elf"),
    ]:
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

def _load_optional_json_artifact(
    state: Dict[str, Any],
    workflow_dir: str,
    prefixes: List[str],
    rel_or_abs: Optional[str],
) -> Dict[str, Any]:
    target = _norm_path(rel_or_abs)
    if not target:
        return {}

    # Local first
    local_path = os.path.join(workflow_dir, target) if workflow_dir and not os.path.isabs(target) else target
    data = _safe_json(local_path)
    if data:
        return data

    # Supabase fallback
    try:
        supabase = _get_supabase(state)
        obj, _ = _load_json_from_supabase(supabase, prefixes, target)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    return {}


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or "")

    print(f"\n⚙️ Running {AGENT_NAME}")

    software_pkg, software_dbg = _resolve_package_from_state_local_or_supabase(
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

    firmware_pkg, firmware_dbg = _resolve_package_from_state_local_or_supabase(
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

    rtl_pkg, rtl_dbg = _resolve_package_from_state_local_or_supabase(
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

    digital_spec_path = ""
    digital_spec_json = {}
    integration_intent_path = ""
    integration_intent_json = {}
    

    existing_cosim = state.get("system_cosim_manifest") or {}
    existing_sw = existing_cosim.get("software") if isinstance(existing_cosim, dict) else {}
    existing_apps = existing_sw.get("applications") if isinstance(existing_sw, dict) else []

    

    register_map_spec = _load_optional_json_artifact(
        state=state,
        workflow_dir=workflow_dir,
        prefixes=firmware_dbg.get("storage_prefixes") or [],
        rel_or_abs=register_map,
    )

    # ---- semantic truth restore (MOVE THIS UP) ----
    digital_spec_path = (
        rtl_pkg.get("digital_spec_json")
        or rtl_pkg.get("digital_spec_path")
        or "spec/digital_subsystem_spec.json"
    )
    digital_spec_json = _load_optional_json_artifact(
        state=state,
        workflow_dir=workflow_dir,
        prefixes=rtl_dbg.get("storage_prefixes") or [],
        rel_or_abs=digital_spec_path,
    )

    integration_intent_path = (
        rtl_pkg.get("integration_intent")
        or rtl_pkg.get("integration_intent_path")
        or "system/integration/system_integration_intent.json"
    )
    integration_intent_json = _load_optional_json_artifact(
        state=state,
        workflow_dir=workflow_dir,
        prefixes=rtl_dbg.get("storage_prefixes") or [],
        rel_or_abs=integration_intent_path,
    )

    validation_spec = {
        "software": {
            "applications": existing_apps if isinstance(existing_apps, list) else [],
            "entry": software_entry,
        },
        "firmware": {
            "register_map_path": register_map or "",
            "register_map_spec": register_map_spec,
            "interrupts": interrupts,
        },
        "rtl": {
            "top": top_sim or "",
            "sim_filelist": sim_filelist,
            "digital_spec_path": digital_spec_path,
            "digital_spec_json": digital_spec_json,
            "integration_intent_path": integration_intent_path,
            "integration_intent_json": integration_intent_json,
        },
    }


    # ---- NOW SAFE TO USE ----
    semantic_ready = bool(digital_spec_json and integration_intent_json)

    ready_for_l2_contract = bool(
        software_pkg and
        firmware_pkg and
        rtl_pkg and
        compile_sim and
        rtl_ready_for_cosim and
        sim_filelist and
        semantic_ready
    )

    

    manifest: Dict[str, Any] = {
        "validation_scope": "full_stack",
        "generated_at": _now(),
        "agent": AGENT_NAME,
        "software": {
            "package_present": bool(software_pkg),
            "entry": software_entry,
            "applications": existing_apps if isinstance(existing_apps, list) else [],
            "package_type": software_pkg.get("package_type"),
            "l1_validation_status": software_validation_l1_status,
            "l1_ready": software_l1_ready,
        },
        "firmware": {
            "package_present": bool(firmware_pkg),
            "elf": firmware_elf,
            "register_map": register_map,
            "register_map_spec": register_map_spec,
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
        "validation_spec": validation_spec,

        "readiness": {
            "software_present": bool(software_pkg),
            "firmware_present": bool(firmware_pkg),
            "rtl_present": bool(rtl_pkg),
            "rtl_sim_ready": bool(compile_sim and rtl_ready_for_cosim and sim_filelist),
            "semantic_ready": semantic_ready,
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
            "digital_spec_found": bool(digital_spec_json),
            "integration_intent_found": bool(integration_intent_json),
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

    # Canonical L2 ingest output for downstream agents.
    state["system_cosim_manifest"] = manifest
    state["system_cosim_ingest_debug"] = debug

    # Supabase-first normalized asset view.
    state["system_software_cosim_ingest"] = {
        "software_assets": {
            "package_present": bool(software_pkg),
            "package_type": software_pkg.get("package_type"),
            "package_resolved_path": software_dbg.get("resolved_path") or "",
            "entry": software_entry or "",
            "l1_validation_status": software_validation_l1_status or "",
            "l1_ready": software_l1_ready,
        },
        "firmware_assets": {
            "package_present": bool(firmware_pkg),
            "package_type": firmware_pkg.get("package_type"),
            "package_resolved_path": firmware_dbg.get("resolved_path") or "",
            "elf_path": firmware_elf or "",
            "register_map_path": register_map or "",
            "register_map_spec": register_map_spec,
            "interrupts": interrupts,
            "dma_present": dma_present,
        },
        "rtl_assets": {
            "package_present": bool(rtl_pkg),
            "package_type": rtl_pkg.get("package_type"),
            "package_resolved_path": rtl_dbg.get("resolved_path") or "",
            "top_path": top_sim or "",
            "sim_filelist": sim_filelist,
            "phys_filelist": phys_filelist,
            "lib_filelist": lib_filelist,
            "sim_harness_path": sim_filelist[0] if sim_filelist else "",
            "verilator_makefile_path": "",
            "compile_sim": "pass" if compile_sim else "fail",
            "ready_for_cosim": rtl_ready_for_cosim,
        },
        "semantic_assets": {
            "digital_spec_json_path": digital_spec_path,
            "digital_spec_json": digital_spec_json,
            "integration_intent_path": integration_intent_path,
            "integration_intent_json": integration_intent_json,
        },
        "readiness": manifest.get("readiness") or {},
    }

    state["firmware_elf_path"] = firmware_elf or ""
    state["firmware_register_map_path"] = register_map or ""
    state["firmware_register_map"] = register_map_spec or {}
    state["rtl_top_path"] = top_sim or ""
    state["rtl_sim_harness_path"] = sim_filelist[0] if sim_filelist else ""
    state["rtl_verilator_makefile_path"] = ""
    state["system_cosim_interrupts"] = interrupts
    state["system_cosim_dma_present"] = dma_present
    state["digital_spec_json_path"] = digital_spec_path
    state["digital_spec_json"] = digital_spec_json
    state["system_integration_intent_path"] = integration_intent_path
    state["system_integration_intent"] = integration_intent_json

    state["status"] = (
        "✅ CoSim ingest ready"
        if manifest["readiness"]["ready_for_system_l2_contract"]
        else "⚠️ CoSim ingest incomplete"
    )
    return state

    