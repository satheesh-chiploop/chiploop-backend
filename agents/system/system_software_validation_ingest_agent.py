import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Validation Ingest Agent"
OUTPUT_SUBDIR = "system/software_validation/input"

MANIFEST_JSON = "system_software_validation_manifest.json"
SUMMARY_MD = "system_software_validation_summary.md"
DEBUG_JSON = "system_software_validation_debug.json"

ARTIFACT_BUCKET_CANDIDATES = ["artifacts"]

SOFTWARE_PACKAGE_SUFFIX_CANDIDATES = [
    "system/software/package/system_software_package.json",
    "system_software_package.json",
]

BUILD_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/build/system_software_build_manifest.json",
]

TEST_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/tests/system_software_test_manifest.json",
]

MOCK_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/mock/system_software_mock_manifest.json",
]

SDK_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/sdk/system_software_sdk_manifest.json",
]

APP_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/apps/system_software_application_manifest.json",
]

TOOLS_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/tools/system_software_tools_manifest.json",
]

ADAPTER_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/adapter/system_software_adapter_manifest.json",
]

SERVICES_MANIFEST_SUFFIX_CANDIDATES = [
    "system/software/services/system_software_core_services_manifest.json",
]

INPUT_CONTRACT_SUFFIX_CANDIDATES = [
    "system/software/input/system_software_input_contract.json",
]

API_CONTRACT_SUFFIX_CANDIDATES = [
    "system/software/sdk/system_software_api_contract.json",
]

EXECUTIVE_SUMMARY_SUFFIX_CANDIDATES = [
    "system/software/executive/system_software_executive_summary.json",
]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


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
        "system_software_workflow_id",
        "software_workflow_id",
        "source_software_workflow_id",
        "source_workflow_id",
    ):
        value = state.get(key)
        if _is_nonempty_str(value):
            return str(value).strip()

    system_block = state.get("system") or {}
    if isinstance(system_block, dict):
        for key in ("software_workflow_id", "source_software_workflow_id"):
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
    app_name = state.get("app_name") or "system_software_validation"

    explicit_prefix = state.get("source_artifact_prefix")
    if _is_nonempty_str(explicit_prefix):
        prefixes.append(_norm_path(explicit_prefix).rstrip("/") + "/")

    prefixes.append(f"backend/workflows/{source_workflow_id}/")

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
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        return _safe_json_loads(text)
    except Exception:
        return {}


def _storage_exists_and_load_json(supabase, prefixes: List[str], relative_or_abs: str) -> Tuple[str, Dict[str, Any], str]:
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


def _storage_exists_only(supabase, prefixes: List[str], relative_or_abs: str) -> Tuple[bool, str, str]:
    target = _norm_path(relative_or_abs)
    if not target:
        return False, "", ""

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
            try:
                supabase.storage.from_(bucket).download(full_path)
                return True, bucket, full_path
            except Exception:
                pass

    return False, "", ""


def _candidate_values(state: Dict[str, Any], explicit_keys: List[str], suffix_candidates: List[str]) -> List[str]:
    values: List[str] = []

    for key in explicit_keys:
        value = state.get(key)
        if _is_nonempty_str(value):
            values.append(_norm_path(value))

    system_block = state.get("system")
    if isinstance(system_block, dict):
        for key in explicit_keys:
            value = system_block.get(key)
            if _is_nonempty_str(value):
                values.append(_norm_path(value))

    values.extend(suffix_candidates)

    seen = set()
    out: List[str] = []
    for item in values:
        item = _norm_path(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_json_manifest(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    explicit_state_key: str,
    path_keys: List[str],
    suffix_candidates: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    obj = state.get(explicit_state_key)
    if isinstance(obj, dict) and obj:
        return obj, {
            "mode": "state",
            "resolved_path": f"state:{explicit_state_key}",
            "bucket": "",
        }

    if source_workflow_id:
        supabase = _get_supabase(state)
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            bucket, found_obj, full_path = _storage_exists_and_load_json(supabase, prefixes, candidate)
            if found_obj:
                return found_obj, {
                    "mode": "supabase",
                    "resolved_path": full_path,
                    "bucket": bucket,
                }

    if workflow_dir:
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            local_path = _join_workflow_path(workflow_dir, candidate)
            found_obj = _safe_read_json(local_path)
            if found_obj:
                return found_obj, {
                    "mode": "local",
                    "resolved_path": local_path,
                    "bucket": "",
                }

    return {}, {
        "mode": "missing",
        "resolved_path": "",
        "bucket": "",
    }


def _resolve_file_presence(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    relative_or_abs: str,
) -> Dict[str, Any]:
    target = _norm_path(relative_or_abs)
    if not target:
        return {
            "path": "",
            "exists": False,
            "source": "missing",
            "resolved_path": "",
            "bucket": "",
        }

    if source_workflow_id:
        supabase = _get_supabase(state)
        exists, bucket, full_path = _storage_exists_only(supabase, prefixes, target)
        if exists:
            return {
                "path": target,
                "exists": True,
                "source": "supabase",
                "resolved_path": full_path,
                "bucket": bucket,
            }

    if workflow_dir:
        local_path = _join_workflow_path(workflow_dir, target)
        if os.path.exists(local_path):
            return {
                "path": target,
                "exists": True,
                "source": "local",
                "resolved_path": local_path,
                "bucket": "",
            }

    return {
        "path": target,
        "exists": False,
        "source": "missing",
        "resolved_path": "",
        "bucket": "",
    }


def _package_files(package_manifest: Dict[str, Any]) -> List[str]:
    files = package_manifest.get("files") or []
    return [str(x).strip() for x in files if _is_nonempty_str(x)]


def _workspace_members(build_manifest: Dict[str, Any]) -> List[str]:
    members = build_manifest.get("workspace_members") or []
    return [str(x).strip() for x in members if _is_nonempty_str(x)]


def _status_for_required_asset(name: str, exists: bool) -> Dict[str, Any]:
    return {
        "name": name,
        "required": True,
        "status": "present" if exists else "missing_required",
    }


def _status_for_optional_asset(name: str, exists: bool) -> Dict[str, Any]:
    return {
        "name": name,
        "required": False,
        "status": "present" if exists else "missing_optional",
    }


def _build_validation_manifest(
    source_workflow_id: str,
    resolution_meta: Dict[str, Any],
    package_manifest: Dict[str, Any],
    package_meta: Dict[str, Any],
    build_manifest: Dict[str, Any],
    build_meta: Dict[str, Any],
    test_manifest: Dict[str, Any],
    test_meta: Dict[str, Any],
    mock_manifest: Dict[str, Any],
    mock_meta: Dict[str, Any],
    sdk_manifest: Dict[str, Any],
    sdk_meta: Dict[str, Any],
    app_manifest: Dict[str, Any],
    app_meta: Dict[str, Any],
    tools_manifest: Dict[str, Any],
    tools_meta: Dict[str, Any],
    adapter_manifest: Dict[str, Any],
    adapter_meta: Dict[str, Any],
    services_manifest: Dict[str, Any],
    services_meta: Dict[str, Any],
    input_contract: Dict[str, Any],
    input_contract_meta: Dict[str, Any],
    api_contract: Dict[str, Any],
    api_contract_meta: Dict[str, Any],
    executive_summary: Dict[str, Any],
    executive_meta: Dict[str, Any],
    package_file_checks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    required_assets = [
        _status_for_required_asset("software_package_manifest", bool(package_manifest)),
        _status_for_required_asset("build_manifest", bool(build_manifest)),
        _status_for_required_asset("sdk_manifest", bool(sdk_manifest)),
        _status_for_required_asset("application_manifest", bool(app_manifest)),
        _status_for_required_asset("input_contract", bool(input_contract)),
        _status_for_required_asset("api_contract", bool(api_contract)),
    ]

    optional_assets = [
        _status_for_optional_asset("test_manifest", bool(test_manifest)),
        _status_for_optional_asset("mock_manifest", bool(mock_manifest)),
        _status_for_optional_asset("tools_manifest", bool(tools_manifest)),
        _status_for_optional_asset("adapter_manifest", bool(adapter_manifest)),
        _status_for_optional_asset("services_manifest", bool(services_manifest)),
        _status_for_optional_asset("executive_summary", bool(executive_summary)),
    ]

    missing_required = [x["name"] for x in required_assets if x["status"] == "missing_required"]
    package_missing_required_files = [x["path"] for x in package_file_checks if x["required"] and not x["exists"]]

    overall_ingest_status = "ready"
    if missing_required or package_missing_required_files:
        overall_ingest_status = "not_ready"

    return {
        "package_type": "system_software_validation_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "validation_scope": "software_only",
        "co_sim_enabled": False,
        "source_workflow_id": source_workflow_id,
        "source_resolution": {
            "mode": resolution_meta.get("mode") or "unknown",
            "artifact_bucket": resolution_meta.get("artifact_bucket") or "",
            "storage_prefixes": resolution_meta.get("storage_prefixes") or [],
            "workflow_row_found": bool(resolution_meta.get("workflow_row_found")),
            "workflow_name": resolution_meta.get("workflow_name") or "",
        },
        "artifact_roots": {
            "software_root": "system/software",
            "validation_root": OUTPUT_SUBDIR,
            "package_root": "system/software/package",
            "build_root": "system/software/build",
            "tests_root": "system/software/tests",
            "mock_root": "system/software/mock",
        },
        "discovered_assets": {
            "software_package_manifest": {
                "exists": bool(package_manifest),
                "source": package_meta.get("mode"),
                "resolved_path": package_meta.get("resolved_path"),
                "bucket": package_meta.get("bucket"),
            },
            "build_manifest": {
                "exists": bool(build_manifest),
                "source": build_meta.get("mode"),
                "resolved_path": build_meta.get("resolved_path"),
                "bucket": build_meta.get("bucket"),
            },
            "test_manifest": {
                "exists": bool(test_manifest),
                "source": test_meta.get("mode"),
                "resolved_path": test_meta.get("resolved_path"),
                "bucket": test_meta.get("bucket"),
            },
            "mock_manifest": {
                "exists": bool(mock_manifest),
                "source": mock_meta.get("mode"),
                "resolved_path": mock_meta.get("resolved_path"),
                "bucket": mock_meta.get("bucket"),
            },
            "sdk_manifest": {
                "exists": bool(sdk_manifest),
                "source": sdk_meta.get("mode"),
                "resolved_path": sdk_meta.get("resolved_path"),
                "bucket": sdk_meta.get("bucket"),
            },
            "application_manifest": {
                "exists": bool(app_manifest),
                "source": app_meta.get("mode"),
                "resolved_path": app_meta.get("resolved_path"),
                "bucket": app_meta.get("bucket"),
            },
            "tools_manifest": {
                "exists": bool(tools_manifest),
                "source": tools_meta.get("mode"),
                "resolved_path": tools_meta.get("resolved_path"),
                "bucket": tools_meta.get("bucket"),
            },
            "adapter_manifest": {
                "exists": bool(adapter_manifest),
                "source": adapter_meta.get("mode"),
                "resolved_path": adapter_meta.get("resolved_path"),
                "bucket": adapter_meta.get("bucket"),
            },
            "services_manifest": {
                "exists": bool(services_manifest),
                "source": services_meta.get("mode"),
                "resolved_path": services_meta.get("resolved_path"),
                "bucket": services_meta.get("bucket"),
            },
            "input_contract": {
                "exists": bool(input_contract),
                "source": input_contract_meta.get("mode"),
                "resolved_path": input_contract_meta.get("resolved_path"),
                "bucket": input_contract_meta.get("bucket"),
            },
            "api_contract": {
                "exists": bool(api_contract),
                "source": api_contract_meta.get("mode"),
                "resolved_path": api_contract_meta.get("resolved_path"),
                "bucket": api_contract_meta.get("bucket"),
            },
            "executive_summary": {
                "exists": bool(executive_summary),
                "source": executive_meta.get("mode"),
                "resolved_path": executive_meta.get("resolved_path"),
                "bucket": executive_meta.get("bucket"),
            },
        },
        "crate_targets": {
            "workspace_members": _workspace_members(build_manifest),
            "sdk_crate_name": sdk_manifest.get("crate_name") or "",
            "app_names": sdk_manifest.get("app_names") or [],
            "service_names": services_manifest.get("service_names") or [],
        },
        "validation_inputs": {
            "required_assets": required_assets,
            "optional_assets": optional_assets,
            "package_file_checks": package_file_checks,
        },
        "validation_readiness": {
            "missing_required_assets": missing_required,
            "missing_required_package_files": package_missing_required_files,
            "overall_ingest_status": overall_ingest_status,
        },
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    readiness = manifest.get("validation_readiness") or {}
    discovered = manifest.get("discovered_assets") or {}
    lines = [
        "# System Software Validation Ingest Summary",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: `{manifest.get('source_workflow_id') or 'unavailable'}`",
        f"- Validation scope: `{manifest.get('validation_scope')}`",
        f"- Co-sim enabled: `{manifest.get('co_sim_enabled')}`",
        f"- Overall ingest status: `{readiness.get('overall_ingest_status')}`",
        "",
        "## Discovered assets",
        "",
    ]

    for key, item in discovered.items():
        lines.append(
            f"- `{key}` → exists=`{item.get('exists')}` | source=`{item.get('source')}` | resolved=`{item.get('resolved_path') or ''}`"
        )

    lines.extend([
        "",
        "## Workspace members",
        "",
    ])

    for member in (((manifest.get("crate_targets") or {}).get("workspace_members")) or []):
        lines.append(f"- `{member}`")
    if not (((manifest.get("crate_targets") or {}).get("workspace_members")) or []):
        lines.append("- none discovered")

    lines.extend([
        "",
        "## Missing required assets",
        "",
    ])

    missing_required_assets = readiness.get("missing_required_assets") or []
    missing_required_package_files = readiness.get("missing_required_package_files") or []

    if not missing_required_assets and not missing_required_package_files:
        lines.append("- none")
    else:
        for item in missing_required_assets:
            lines.append(f"- required manifest missing: `{item}`")
        for item in missing_required_package_files:
            lines.append(f"- required package file missing: `{item}`")

    lines.append("")
    return "\n".join(lines)

def _storage_download_bytes(supabase, bucket: str, path: str) -> bytes:
    try:
        data = supabase.storage.from_(bucket).download(path)
        return data if isinstance(data, bytes) else str(data).encode("utf-8")
    except Exception:
        return b""


def _restore_package_files_locally(
    state: Dict[str, Any],
    workflow_dir: str,
    package_file_checks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    restored_root = os.path.join(workflow_dir, "restored_system_software")
    os.makedirs(restored_root, exist_ok=True)

    restored_files = []
    failed_files = []

    supabase = _get_supabase(state)

    for item in package_file_checks:
        rel_path = _norm_path(item.get("path"))
        bucket = _norm_path(item.get("bucket"))
        resolved_path = _norm_path(item.get("resolved_path"))

        if not rel_path or not bucket or not resolved_path:
            continue

        blob = _storage_download_bytes(supabase, bucket, resolved_path)
        if not blob:
            failed_files.append(rel_path)
            continue

        local_path = os.path.join(restored_root, rel_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(blob)

        restored_files.append(rel_path)

    return {
        "restored_root": restored_root,
        "restored_files": restored_files,
        "failed_files": failed_files,
        "build_root": os.path.join(restored_root, "system/software/build"),
        "test_root": os.path.join(restored_root, "system/software/build"),
        "mock_root": os.path.join(restored_root, "system/software/mock"),
    }


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    print(f"\\n📥 Running {AGENT_NAME}")

    source_workflow_id = _get_source_workflow_id(state)
    if not source_workflow_id:
        state["status"] = "❌ system software workflow id missing"
        return state

    resolution_meta: Dict[str, Any] = {
        "mode": "unknown",
        "artifact_bucket": "",
        "storage_prefixes": [],
        "workflow_row_found": False,
        "workflow_name": "",
    }

    prefixes: List[str] = []
    if source_workflow_id:
        try:
            supabase = _get_supabase(state)
            wf_row = _workflow_row(supabase, source_workflow_id)
            prefixes = _workflow_storage_prefixes(state, source_workflow_id, wf_row)
            resolution_meta = {
                "mode": "supabase_or_local",
                "artifact_bucket": "",
                "storage_prefixes": prefixes,
                "workflow_row_found": bool(wf_row),
                "workflow_name": (wf_row or {}).get("name") or "",
            }
        except Exception:
            prefixes = []

    package_manifest, package_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_package",
        path_keys=["system_software_package_path"],
        suffix_candidates=SOFTWARE_PACKAGE_SUFFIX_CANDIDATES,
    )

    build_manifest, build_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_build_manifest",
        path_keys=["system_software_build_manifest_path"],
        suffix_candidates=BUILD_MANIFEST_SUFFIX_CANDIDATES,
    )

    test_manifest, test_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_test_manifest",
        path_keys=["system_software_test_manifest_path"],
        suffix_candidates=TEST_MANIFEST_SUFFIX_CANDIDATES,
    )

    mock_manifest, mock_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_mock_manifest",
        path_keys=["system_software_mock_manifest_path"],
        suffix_candidates=MOCK_MANIFEST_SUFFIX_CANDIDATES,
    )

    sdk_manifest, sdk_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_sdk_manifest",
        path_keys=["system_software_sdk_manifest_path"],
        suffix_candidates=SDK_MANIFEST_SUFFIX_CANDIDATES,
    )

    app_manifest, app_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_application_manifest",
        path_keys=["system_software_application_manifest_path"],
        suffix_candidates=APP_MANIFEST_SUFFIX_CANDIDATES,
    )

    tools_manifest, tools_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_tools_manifest",
        path_keys=["system_software_tools_manifest_path"],
        suffix_candidates=TOOLS_MANIFEST_SUFFIX_CANDIDATES,
    )

    adapter_manifest, adapter_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_adapter_manifest",
        path_keys=["system_software_adapter_manifest_path"],
        suffix_candidates=ADAPTER_MANIFEST_SUFFIX_CANDIDATES,
    )

    services_manifest, services_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_core_services_manifest",
        path_keys=["system_software_core_services_manifest_path"],
        suffix_candidates=SERVICES_MANIFEST_SUFFIX_CANDIDATES,
    )

    input_contract, input_contract_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_input_contract",
        path_keys=["system_software_input_contract_path"],
        suffix_candidates=INPUT_CONTRACT_SUFFIX_CANDIDATES,
    )

    api_contract, api_contract_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_api_contract",
        path_keys=["system_software_api_contract_path"],
        suffix_candidates=API_CONTRACT_SUFFIX_CANDIDATES,
    )

    executive_summary, executive_meta = _resolve_json_manifest(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        explicit_state_key="system_software_executive_summary",
        path_keys=["system_software_executive_summary_path"],
        suffix_candidates=EXECUTIVE_SUMMARY_SUFFIX_CANDIDATES,
    )

    package_file_checks: List[Dict[str, Any]] = []
    for rel_path in _package_files(package_manifest):
        file_meta = _resolve_file_presence(
            state=state,
            workflow_dir=workflow_dir,
            source_workflow_id=source_workflow_id,
            prefixes=prefixes,
            relative_or_abs=rel_path,
        )
        package_file_checks.append({
            "path": rel_path,
            "required": True,
            **file_meta,
        })

    restore_info = _restore_package_files_locally(
        state=state,
        workflow_dir=workflow_dir,
        package_file_checks=package_file_checks,
    )

    
    manifest = _build_validation_manifest(
        source_workflow_id=source_workflow_id,
        resolution_meta=resolution_meta,
        package_manifest=package_manifest,
        package_meta=package_meta,
        build_manifest=build_manifest,
        build_meta=build_meta,
        test_manifest=test_manifest,
        test_meta=test_meta,
        mock_manifest=mock_manifest,
        mock_meta=mock_meta,
        sdk_manifest=sdk_manifest,
        sdk_meta=sdk_meta,
        app_manifest=app_manifest,
        app_meta=app_meta,
        tools_manifest=tools_manifest,
        tools_meta=tools_meta,
        adapter_manifest=adapter_manifest,
        adapter_meta=adapter_meta,
        services_manifest=services_manifest,
        services_meta=services_meta,
        input_contract=input_contract,
        input_contract_meta=input_contract_meta,
        api_contract=api_contract,
        api_contract_meta=api_contract_meta,
        executive_summary=executive_summary,
        executive_meta=executive_meta,
        package_file_checks=package_file_checks,
    )

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workflow_dir": workflow_dir,
        "source_workflow_id": source_workflow_id,
        "resolution_meta": resolution_meta,
        "resolved_assets": {
            "package_meta": package_meta,
            "build_meta": build_meta,
            "test_meta": test_meta,
            "mock_meta": mock_meta,
            "sdk_meta": sdk_meta,
            "app_meta": app_meta,
            "tools_meta": tools_meta,
            "adapter_meta": adapter_meta,
            "services_meta": services_meta,
            "input_contract_meta": input_contract_meta,
            "api_contract_meta": api_contract_meta,
            "executive_meta": executive_meta,
        },
        "package_file_check_count": len(package_file_checks),
        "missing_required_package_files": [
            item["path"] for item in package_file_checks if not item.get("exists")
        ],
        "restore_info": restore_info,
    }

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    

    state["source_software_workflow_id"] = source_workflow_id

    state["system_software_validation_manifest"] = manifest
    state["system_software_validation_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["system_software_validation_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"

    state["system_software_package"] = package_manifest or state.get("system_software_package") or {}
    state["system_software_build_manifest"] = build_manifest or state.get("system_software_build_manifest") or {}
    state["system_software_test_manifest"] = test_manifest or state.get("system_software_test_manifest") or {}
    state["system_software_mock_manifest"] = mock_manifest or state.get("system_software_mock_manifest") or {}
    state["system_software_sdk_manifest"] = sdk_manifest or state.get("system_software_sdk_manifest") or {}
    state["system_software_application_manifest"] = app_manifest or state.get("system_software_application_manifest") or {}
    state["system_software_tools_manifest"] = tools_manifest or state.get("system_software_tools_manifest") or {}
    state["system_software_adapter_manifest"] = adapter_manifest or state.get("system_software_adapter_manifest") or {}
    state["system_software_core_services_manifest"] = services_manifest or state.get("system_software_core_services_manifest") or {}
    state["system_software_input_contract"] = input_contract or state.get("system_software_input_contract") or {}
    state["system_software_api_contract"] = api_contract or state.get("system_software_api_contract") or {}
    state["system_software_executive_summary"] = executive_summary or state.get("system_software_executive_summary") or {}

    state["system_software_validation_package_file_checks"] = package_file_checks

    state["system_software_validation_local_root"] = restore_info.get("restored_root") or ""
    state["system_software_build_root"] = restore_info.get("build_root") or ""
    state["system_software_test_root"] = restore_info.get("test_root") or ""
    state["system_software_mock_root"] = restore_info.get("mock_root") or ""
    state["system_software_restored_files"] = restore_info.get("restored_files") or []
    state["system_software_restore_failed_files"] = restore_info.get("failed_files") or []

    system_block = state.setdefault("system", {})
    if isinstance(system_block, dict):
        system_block["software_validation_manifest"] = manifest
        system_block["software_validation_manifest_path"] = state["system_software_validation_manifest_path"]
        system_block["source_software_workflow_id"] = source_workflow_id
        system_block["system_software_validation_local_root"] = state["system_software_validation_local_root"]
        system_block["system_software_build_root"] = state["system_software_build_root"]
        system_block["system_software_test_root"] = state["system_software_test_root"]
        system_block["system_software_mock_root"] = state["system_software_mock_root"]

    ingest_status = ((manifest.get("validation_readiness") or {}).get("overall_ingest_status")) or "not_ready"
    state["status"] = (
        "✅ System software validation ingest complete"
        if ingest_status == "ready"
        else "⚠️ System software validation ingest completed with missing required inputs"
    )
    return state