import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Handoff Ingest Agent"
OUTPUT_SUBDIR = "system/software/input"
CONTRACT_JSON = "system_software_input_contract.json"
SUMMARY_MD = "system_software_input_summary.md"
DEBUG_JSON = "system_software_input_debug.json"

ARTIFACT_BUCKET_CANDIDATES = ["artifacts"]
HANDOFF_SUFFIX_CANDIDATES = [
    "system/software_handoff/system_software_handoff.json",
    "system_software_handoff.json",
]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_json_loads(text: Any) -> Dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _join_workflow_path(workflow_dir: str, rel_or_abs: str) -> str:
    if not rel_or_abs:
        return ""
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.abspath(os.path.join(workflow_dir, rel_or_abs))


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    try:
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


def _get_source_workflow_id(state: Dict[str, Any]) -> str:
    for key in (
        "system_firmware_workflow_id",
        "firmware_workflow_id",
        "source_firmware_workflow_id",
        "source_workflow_id",
    ):
        value = state.get(key)
        if _is_nonempty_str(value):
            return str(value).strip()
    system_block = state.get("system") or {}
    if isinstance(system_block, dict):
        for key in ("firmware_workflow_id", "source_firmware_workflow_id"):
            value = system_block.get(key)
            if _is_nonempty_str(value):
                return str(value).strip()
    return ""


def _workflow_row(supabase, workflow_id: str) -> Dict[str, Any]:
    if not workflow_id:
        return {}
    try:
        resp = (
            supabase.table("workflows")
            .select("id,user_id,name,loop_type,artifacts,definitions")
            .eq("id", workflow_id)
            .single()
            .execute()
        )
        return resp.data or {}
    except Exception:
        return {}


def _workflow_storage_prefixes(state: Dict[str, Any], source_workflow_id: str, workflow_row: Dict[str, Any]) -> List[str]:
    prefixes: List[str] = []
    user_id = (workflow_row or {}).get("user_id")
    loop_type = (workflow_row or {}).get("loop_type") or "system"
    app_name = state.get("app_name") or "system_software"

    explicit_prefix = state.get("source_artifact_prefix")
    if _is_nonempty_str(explicit_prefix):
        prefixes.append(_norm_path(explicit_prefix).rstrip("/") + "/")

    # Most robust current convention in your backend for app runs.
    prefixes.append(f"backend/workflows/{source_workflow_id}/")

    # Fallbacks for older local mirroring conventions.
    if _is_nonempty_str(user_id):
        prefixes.append(f"{user_id}/{source_workflow_id}/")
        prefixes.append(f"artifacts/{user_id}/{source_workflow_id}/")
        prefixes.append(f"backend/workflows/{source_workflow_id}/{loop_type}/{app_name}/")

    seen = set()
    out: List[str] = []
    for prefix in prefixes:
        norm = _norm_path(prefix)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _storage_download_json(supabase, bucket: str, path: str) -> Dict[str, Any]:
    try:
        data = supabase.storage.from_(bucket).download(path)
        if isinstance(data, bytes):
            text = data.decode("utf-8")
        else:
            text = str(data)
        return _safe_json_loads(text)
    except Exception:
        return {}


def _storage_exists_and_load(supabase, prefixes: List[str], relative_or_abs: str) -> Tuple[str, Dict[str, Any], str]:
    target = _norm_path(relative_or_abs)
    if not target:
        return "", {}, ""

    candidate_paths: List[str] = []
    if target.startswith("backend/workflows/") or target.startswith("artifacts/"):
        candidate_paths.append(target)
    else:
        for prefix in prefixes:
            candidate_paths.append(f"{prefix.rstrip('/')}/{target}")
        candidate_paths.append(target)

    seen = set()
    ordered: List[str] = []
    for p in candidate_paths:
        p = _norm_path(p)
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)

    for bucket in ARTIFACT_BUCKET_CANDIDATES:
        for full_path in ordered:
            obj = _storage_download_json(supabase, bucket, full_path)
            if obj:
                return bucket, obj, full_path
    return "", {}, ""


def _candidate_handoff_paths(state: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    for key in (
        "system_software_handoff_path",
        "software_handoff_path",
        "system_firmware_handoff_path",
    ):
        value = state.get(key)
        if _is_nonempty_str(value):
            candidates.append(_norm_path(value))

    system_block = state.get("system")
    if isinstance(system_block, dict):
        for key in ("software_handoff_path", "system_software_handoff_path"):
            value = system_block.get(key)
            if _is_nonempty_str(value):
                candidates.append(_norm_path(value))

    candidates.extend(HANDOFF_SUFFIX_CANDIDATES)
    return candidates


def _resolve_handoff_manifest(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if isinstance(state.get("system_software_handoff"), dict) and state.get("system_software_handoff"):
        return state["system_software_handoff"], {
            "resolution_mode": "state",
            "handoff_storage_path": state.get("system_software_handoff_storage_path") or "state:system_software_handoff",
            "source_workflow_id": state.get("system_firmware_workflow_id") or state.get("source_firmware_workflow_id") or "",
            "workflow_row_found": False,
            "storage_prefixes": [],
            "artifact_bucket": state.get("system_software_handoff_bucket") or "",
        }

    source_workflow_id = _get_source_workflow_id(state)
    workflow_dir = state.get("workflow_dir") or ""

    if source_workflow_id:
        supabase = _get_supabase(state)
        wf_row = _workflow_row(supabase, source_workflow_id)
        prefixes = _workflow_storage_prefixes(state, source_workflow_id, wf_row)

        for candidate in _candidate_handoff_paths(state):
            bucket, obj, full_path = _storage_exists_and_load(supabase, prefixes, candidate)
            if obj:
                return obj, {
                    "resolution_mode": "supabase",
                    "handoff_storage_path": full_path,
                    "source_workflow_id": source_workflow_id,
                    "workflow_row_found": bool(wf_row),
                    "storage_prefixes": prefixes,
                    "artifact_bucket": bucket,
                    "workflow_name": wf_row.get("name") if isinstance(wf_row, dict) else None,
                }

    # Local fallback only if present.
    if workflow_dir:
        workflow_dir = os.path.abspath(workflow_dir)
        for candidate in _candidate_handoff_paths(state):
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj, {
                    "resolution_mode": "local",
                    "handoff_storage_path": candidate,
                    "source_workflow_id": source_workflow_id,
                    "workflow_row_found": False,
                    "storage_prefixes": [],
                    "artifact_bucket": "",
                }

    return {}, {
        "resolution_mode": "missing",
        "handoff_storage_path": "",
        "source_workflow_id": source_workflow_id,
        "workflow_row_found": False,
        "storage_prefixes": [],
        "artifact_bucket": "",
    }


def _validate_handoff_manifest(obj: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    if not obj:
        errors.append("handoff_manifest_missing_or_invalid_json")
        return errors

    if obj.get("package_type") != "system_software_handoff":
        errors.append("unexpected_package_type")

    if not _is_nonempty_str(obj.get("package_version")):
        errors.append("package_version_missing")

    system_contract = obj.get("system_contract")
    if not isinstance(system_contract, dict):
        errors.append("system_contract_missing")
    else:
        if not _is_nonempty_str(system_contract.get("top_module")):
            errors.append("top_module_missing")
        if not _is_nonempty_str(system_contract.get("system_integration_intent_path")):
            errors.append("system_integration_intent_path_missing")

    firmware_contract = obj.get("firmware_contract")
    if not isinstance(firmware_contract, dict):
        errors.append("firmware_contract_missing")
    else:
        for key in ("firmware_manifest_path", "register_map_path", "hal_path", "driver_path"):
            if not _is_nonempty_str(firmware_contract.get(key)):
                errors.append(f"{key}_missing")

    if not isinstance(obj.get("software_inputs"), dict):
        errors.append("software_inputs_missing")

    return errors


def _fetch_json_artifact_from_handoff(
    supabase,
    prefixes: List[str],
    artifact_path: str,
    workflow_dir: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if _is_nonempty_str(artifact_path):
        bucket, obj, full_path = _storage_exists_and_load(supabase, prefixes, artifact_path)
        if obj:
            return obj, {"mode": "supabase", "resolved_path": full_path, "bucket": bucket}

    if workflow_dir and _is_nonempty_str(artifact_path):
        full_local = _join_workflow_path(workflow_dir, artifact_path)
        obj = _safe_read_json(full_local)
        if obj:
            return obj, {"mode": "local", "resolved_path": full_local, "bucket": ""}

    return {}, {"mode": "missing", "resolved_path": artifact_path or "", "bucket": ""}


def _resolve_artifact_presence_only(
    supabase,
    prefixes: List[str],
    artifact_path: str,
    workflow_dir: str,
) -> Dict[str, Any]:
    target = _norm_path(artifact_path)
    if not target:
        return {
            "mode": "missing",
            "resolved_path": "",
            "bucket": "",
            "exists": False,
        }

    candidate_paths: List[str] = []
    if target.startswith("backend/workflows/") or target.startswith("artifacts/"):
        candidate_paths.append(target)
    else:
        for prefix in prefixes:
            candidate_paths.append(f"{prefix.rstrip('/')}/{target}")
        candidate_paths.append(target)

    seen = set()
    ordered: List[str] = []
    for p in candidate_paths:
        p = _norm_path(p)
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)

    if supabase is not None:
        for bucket in ARTIFACT_BUCKET_CANDIDATES:
            for full_path in ordered:
                try:
                    supabase.storage.from_(bucket).download(full_path)
                    return {
                        "mode": "supabase",
                        "resolved_path": full_path,
                        "bucket": bucket,
                        "exists": True,
                    }
                except Exception:
                    pass

    if workflow_dir and _is_nonempty_str(artifact_path):
        full_local = _join_workflow_path(workflow_dir, artifact_path)
        if os.path.exists(full_local):
            return {
                "mode": "local",
                "resolved_path": full_local,
                "bucket": "",
                "exists": True,
            }

    return {
        "mode": "missing",
        "resolved_path": artifact_path or "",
        "bucket": "",
        "exists": False,
    }


def _build_artifact_resolution_map(
    handoff_manifest: Dict[str, Any],
    workflow_dir: str,
    state: Dict[str, Any],
    resolution_meta: Dict[str, Any],
) -> Dict[str, Any]:
    source_workflow_id = resolution_meta.get("source_workflow_id") or handoff_manifest.get("source_workflow_id") or ""
    prefixes = resolution_meta.get("storage_prefixes") or []
    supabase = _get_supabase(state) if source_workflow_id else None

    resolved: Dict[str, Any] = {}
    hydrated: Dict[str, Any] = {}

    sections = {
        "system_contract": handoff_manifest.get("system_contract") or {},
        "firmware_contract": handoff_manifest.get("firmware_contract") or {},
        "verification_contract": handoff_manifest.get("verification_contract") or {},
    }

    for section_name, section in sections.items():
        if not isinstance(section, dict):
            continue

        for key, value in section.items():
            if not (_is_nonempty_str(key) and _is_nonempty_str(value) and key.endswith("_path")):
                continue

            resolved_key = f"{section_name}.{key}"
            value_str = str(value).strip()
            is_json = value_str.lower().endswith(".json")

            if is_json:
                if supabase is not None:
                    obj, meta = _fetch_json_artifact_from_handoff(supabase, prefixes, value_str, workflow_dir)
                else:
                    obj, meta = _fetch_json_artifact_from_handoff(None, [], value_str, workflow_dir)

                resolved[resolved_key] = {
                    **meta,
                    "exists": bool(obj) or bool(meta.get("resolved_path")),
                    "artifact_kind": "json",
                }

                if obj:
                    hydrated[resolved_key] = obj
            else:
                meta = _resolve_artifact_presence_only(
                    supabase=supabase,
                    prefixes=prefixes,
                    artifact_path=value_str,
                    workflow_dir=workflow_dir,
                )
                resolved[resolved_key] = {
                    **meta,
                    "artifact_kind": "file",
                }

    return {"resolved": resolved, "hydrated": hydrated}



def _artifact_meta_for_key(
    hydrated_map: Dict[str, Any],
    resolved_map: Dict[str, Any],
    section_name: str,
    key: str,
    raw_path: Any,
) -> Dict[str, Any]:
    fq_key = f"{section_name}.{key}"
    hydrated = hydrated_map.get(fq_key)
    meta = resolved_map.get(fq_key) or {}

    return {
        "path": _norm_path(raw_path),
        "exists": bool(meta.get("exists") or (isinstance(hydrated, dict) and hydrated)),
        "source": meta.get("mode") or "missing",
        "resolved_path": meta.get("resolved_path") or "",
        "bucket": meta.get("bucket") or "",
        "artifact_kind": meta.get("artifact_kind") or ("json" if isinstance(hydrated, dict) and hydrated else "file"),
    }


def _build_input_contract(
    handoff_manifest: Dict[str, Any],
    handoff_path: str,
    errors: List[str],
    source_meta: Dict[str, Any],
    hydrated_map: Dict[str, Any],
    resolved_map: Dict[str, Any],
) -> Dict[str, Any]:
    system_contract = handoff_manifest.get("system_contract") or {}
    firmware_contract = handoff_manifest.get("firmware_contract") or {}
    verification_contract = handoff_manifest.get("verification_contract") or {}
    software_inputs = handoff_manifest.get("software_inputs") or {}
    readiness = handoff_manifest.get("software_readiness") or {}

    required_artifacts: Dict[str, Any] = {}
    for key in software_inputs.get("required_inputs", []) or []:
        source_value = None
        source_section = ""
        if key in firmware_contract:
            source_section = "firmware_contract"
            source_value = firmware_contract.get(key)
        elif key in system_contract:
            source_section = "system_contract"
            source_value = system_contract.get(key)
        elif key in verification_contract:
            source_section = "verification_contract"
            source_value = verification_contract.get(key)

        if source_section:
            required_artifacts[key] = _artifact_meta_for_key(
                hydrated_map, resolved_map, source_section, key, source_value
            )
        else:
            required_artifacts[key] = {
                "path": _norm_path(source_value),
                "exists": False,
                "source": "missing",
                "resolved_path": "",
                "bucket": "",
            }

    recommended_artifacts: Dict[str, Any] = {}
    for key in software_inputs.get("recommended_inputs", []) or []:
        source_value = None
        source_section = ""
        if key in firmware_contract:
            source_section = "firmware_contract"
            source_value = firmware_contract.get(key)
        elif key in system_contract:
            source_section = "system_contract"
            source_value = system_contract.get(key)
        elif key in verification_contract:
            source_section = "verification_contract"
            source_value = verification_contract.get(key)

        if isinstance(source_value, list):
            recommended_artifacts[key] = {"path": "", "paths": source_value, "exists": False, "source": "list", "resolved_path": "", "bucket": ""}
        elif source_section:
            meta = _artifact_meta_for_key(hydrated_map, resolved_map, source_section, key, source_value)
            recommended_artifacts[key] = {**meta, "paths": []}
        else:
            recommended_artifacts[key] = {"path": _norm_path(source_value), "paths": [], "exists": False, "source": "missing", "resolved_path": "", "bucket": ""}

    return {
        "package_type": "system_software_input_contract",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_handoff_manifest_path": handoff_path,
        "source_workflow_id": source_meta.get("source_workflow_id") or handoff_manifest.get("source_workflow_id"),
        "source_workflow_type": handoff_manifest.get("source_workflow_type"),
        "source_resolution": {
            "mode": source_meta.get("resolution_mode"),
            "artifact_bucket": source_meta.get("artifact_bucket") or "",
            "storage_prefixes": source_meta.get("storage_prefixes") or [],
            "workflow_row_found": bool(source_meta.get("workflow_row_found")),
        },
        "input_status": {
            "valid": len(errors) == 0,
            "errors": errors,
            "package_quality": readiness.get("package_quality"),
            "blocking_gaps": readiness.get("blocking_gaps") or [],
            "assumptions": readiness.get("assumptions") or [],
        },
        "system_contract": system_contract,
        "firmware_contract": firmware_contract,
        "verification_contract": verification_contract,
        "required_artifacts": required_artifacts,
        "recommended_artifacts": recommended_artifacts,
        "public_api_candidates": software_inputs.get("public_api_candidates") or [],
    }


def _markdown_report(contract: Dict[str, Any]) -> str:
    status = contract.get("input_status") or {}
    source = contract.get("source_resolution") or {}
    lines = [
        "# System Software Input Contract Summary",
        "",
        f"- Generated at: {contract.get('generated_at')}",
        f"- Source handoff: `{contract.get('source_handoff_manifest_path') or 'missing'}`",
        f"- Source workflow id: `{contract.get('source_workflow_id') or 'unavailable'}`",
        f"- Resolution mode: `{source.get('mode') or 'unknown'}`",
        f"- Valid: `{status.get('valid')}`",
        "",
        "## Required artifacts",
        "",
    ]

    required = contract.get("required_artifacts") or {}
    if required:
        for key, item in required.items():
            lines.append(
                f"- `{key}` → `{item.get('path') or 'missing'}` | exists=`{item.get('exists')}` | source=`{item.get('source')}`"
            )
    else:
        lines.append("- (none)")

    lines.extend(["", "## Validation", ""])
    errors = status.get("errors") or []
    if errors:
        for err in errors:
            lines.append(f"- {err}")
    else:
        lines.append("- no validation errors")

    lines.extend(["", "## Assumptions / gaps", ""])
    for item in (status.get("assumptions") or []) + (status.get("blocking_gaps") or []):
        lines.append(f"- {item}")
    if not ((status.get("assumptions") or []) or (status.get("blocking_gaps") or [])):
        lines.append("- none")

    lines.extend([
        "",
        "## Conclusion",
        "",
        "This artifact is the normalized, validated input contract for System_Software. Downstream software agents should consume the hydrated state objects and this contract rather than scraping firmware artifacts directly.",
        "",
    ])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n📥 Running {AGENT_NAME}")

    handoff_obj, source_meta = _resolve_handoff_manifest(state)
    if not handoff_obj:
        state["status"] = "❌ system software handoff missing (state/supabase/local lookup failed)"
        return state

    errors = _validate_handoff_manifest(handoff_obj)
    artifact_resolution = _build_artifact_resolution_map(handoff_obj, workflow_dir, state, source_meta)
    contract = _build_input_contract(
        handoff_manifest=handoff_obj,
        handoff_path=source_meta.get("handoff_storage_path") or "",
        errors=errors,
        source_meta=source_meta,
        hydrated_map=artifact_resolution.get("hydrated") or {},
        resolved_map=artifact_resolution.get("resolved") or {},
    )



    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workflow_dir": workflow_dir,
        "source_meta": source_meta,
        "validation_errors": errors,
        "resolved_artifacts": artifact_resolution.get("resolved") or {},
        "hydrated_artifact_keys": sorted(list((artifact_resolution.get("hydrated") or {}).keys())),
        "non_json_resolved_keys": sorted([
            k for k, v in (artifact_resolution.get("resolved") or {}).items()
            if (v or {}).get("artifact_kind") == "file" and (v or {}).get("exists")
        ]),
    }

    _record_text(workflow_id, CONTRACT_JSON, json.dumps(contract, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_report(contract))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_handoff"] = handoff_obj
    state["system_software_handoff_storage_path"] = source_meta.get("handoff_storage_path") or ""
    state["source_firmware_workflow_id"] = source_meta.get("source_workflow_id") or contract.get("source_workflow_id") or ""

    hydrated = artifact_resolution.get("hydrated") or {}
    resolved = artifact_resolution.get("resolved") or {}
    firmware_contract = handoff_obj.get("firmware_contract") or {}
    system_contract = handoff_obj.get("system_contract") or {}
    verification_contract = handoff_obj.get("verification_contract") or {}

    # Hydrated JSON objects
    state["system_integration_intent"] = hydrated.get("system_contract.system_integration_intent_path") or state.get("system_integration_intent") or {}
    state["system_firmware_manifest"] = hydrated.get("firmware_contract.firmware_manifest_path") or state.get("system_firmware_manifest") or {}
    state["system_register_map"] = hydrated.get("firmware_contract.register_map_path") or state.get("system_register_map") or {}
    state["system_hal_contract"] = hydrated.get("firmware_contract.hal_path") or state.get("system_hal_contract") or {}
    state["system_driver_contract"] = hydrated.get("firmware_contract.driver_path") or state.get("system_driver_contract") or {}
    state["system_interrupt_mapping"] = hydrated.get("firmware_contract.interrupt_mapping_path") or state.get("system_interrupt_mapping") or {}
    state["system_dma_integration"] = hydrated.get("firmware_contract.dma_integration_path") or state.get("system_dma_integration") or {}
    state["system_boot_dependency_plan"] = hydrated.get("firmware_contract.boot_dependency_plan_path") or state.get("system_boot_dependency_plan") or {}
    state["system_clock_config"] = hydrated.get("firmware_contract.clock_config_path") or state.get("system_clock_config") or {}
    state["system_reset_sequence"] = hydrated.get("firmware_contract.reset_sequence_path") or state.get("system_reset_sequence") or {}
    state["system_power_mode"] = hydrated.get("firmware_contract.power_mode_path") or state.get("system_power_mode") or {}

    # Always preserve raw published paths too, even for non-JSON artifacts
    state["system_integration_intent_path"] = system_contract.get("system_integration_intent_path") or state.get("system_integration_intent_path") or ""
    state["system_firmware_manifest_path"] = firmware_contract.get("firmware_manifest_path") or state.get("system_firmware_manifest_path") or ""
    state["system_register_map_path"] = firmware_contract.get("register_map_path") or state.get("system_register_map_path") or ""
    state["system_hal_path"] = firmware_contract.get("hal_path") or state.get("system_hal_path") or ""
    state["system_driver_path"] = firmware_contract.get("driver_path") or state.get("system_driver_path") or ""
    state["system_interrupt_mapping_path"] = firmware_contract.get("interrupt_mapping_path") or state.get("system_interrupt_mapping_path") or ""
    state["system_dma_path"] = firmware_contract.get("dma_integration_path") or state.get("system_dma_path") or ""
    state["system_boot_dependency_plan_path"] = firmware_contract.get("boot_dependency_plan_path") or state.get("system_boot_dependency_plan_path") or ""
    state["system_clock_config_path"] = firmware_contract.get("clock_config_path") or state.get("system_clock_config_path") or ""
    state["system_reset_sequence_path"] = firmware_contract.get("reset_sequence_path") or state.get("system_reset_sequence_path") or ""
    state["system_power_mode_path"] = firmware_contract.get("power_mode_path") or state.get("system_power_mode_path") or ""
    state["system_cocotb_makefile_path"] = verification_contract.get("cocotb_makefile_path") or state.get("system_cocotb_makefile_path") or ""
    state["system_validation_report_path"] = verification_contract.get("validation_report_path") or state.get("system_validation_report_path") or ""

    # Optional: expose resolved metadata for debugging/future agents
    state["system_software_resolved_artifacts"] = resolved

    state["system_software_input_contract"] = contract
    state["system_software_input_contract_path"] = f"{OUTPUT_SUBDIR}/{CONTRACT_JSON}"
    state["system_software_input_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"

    system_block = state.setdefault("system", {})
    if isinstance(system_block, dict):
        system_block["software_input_contract"] = contract
        system_block["software_input_contract_path"] = state["system_software_input_contract_path"]
        system_block["source_firmware_workflow_id"] = state.get("source_firmware_workflow_id")

    state["status"] = (
        "✅ System software handoff ingested"
        if contract.get("input_status", {}).get("valid")
        else "⚠️ System software handoff ingested with validation issues"
    )
    return state
