import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Packaging Agent"
OUTPUT_SUBDIR = "system/software/package"
MANIFEST_JSON = "system_software_package.json"
SUMMARY_MD = "system_software_package.md"
FILELIST_TXT = "artifact_filelist.txt"
DEBUG_JSON = "system_software_package_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


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

def _file_exists(workflow_dir: str, rel_or_abs: str) -> bool:
    if not _is_nonempty_str(rel_or_abs):
        return False
    candidate = _join_workflow_path(workflow_dir, str(rel_or_abs))
    return bool(candidate and os.path.isfile(candidate))


def _first_nonempty(*values: Any) -> str:
    for value in values:
        s = str(value or "").strip()
        if s:
            return s
    return ""


def _derive_entry_metadata(
    sdk_manifest: Dict[str, Any],
    app_manifest: Dict[str, Any],
    build_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    applications = app_manifest.get("applications") or []
    default_application = _first_nonempty(
        app_manifest.get("entry_application"),
        app_manifest.get("default_application"),
    )

    selected_app: Dict[str, Any] = {}
    if isinstance(applications, list):
        for item in applications:
            if isinstance(item, dict) and _first_nonempty(item.get("app_name")) == default_application:
                selected_app = item
                break

    app_name = _first_nonempty(
        default_application,
        app_manifest.get("application_names", [None])[0] if isinstance(app_manifest.get("application_names"), list) and app_manifest.get("application_names") else "",
    )

    cargo_package = _first_nonempty(
        selected_app.get("cargo_package"),
        app_manifest.get("entry_package"),
        app_manifest.get("package_name"),
    )

    binary_name = _first_nonempty(
        selected_app.get("binary_name"),
        app_manifest.get("entry_binary"),
        cargo_package,
        app_name,
    )

    workspace_members = build_manifest.get("workspace_members") or []
    command = ["cargo", "run", "-p", cargo_package] if cargo_package else []

    entry_resolution_status = "resolved" if app_name and cargo_package and command else "missing"

    return {
        "app_name": app_name,
        "cargo_package": cargo_package,
        "binary_name": binary_name,
        "command": command,
        "working_dir": "system/software",
        "command_source": "package_entry",
        "entry_resolution_status": entry_resolution_status,
        "native_binary_candidates": [
            f"target/debug/{binary_name}",
            f"target/release/{binary_name}",
        ] if binary_name else [],
        "workspace_members": workspace_members,
    }

def _load_manifest(state: Dict[str, Any], workflow_dir: str, state_key: str, path_key: str, fallback: str) -> Dict[str, Any]:
    obj = state.get(state_key)
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get(path_key), fallback):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _collect_files(*manifests: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    seen = set()
    for manifest in manifests:
        for path in manifest.get("files") or []:
            p = str(path).strip().replace("\\", "/")
            if p and p not in seen:
                seen.add(p)
                files.append(p)
    return files


def _extend_if_present(files: List[str], seen: set, *paths: str) -> None:
    for path in paths:
        p = str(path or "").strip().replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            files.append(p)


def _derive_adapter_required_files(adapter_manifest: Dict[str, Any]) -> List[str]:
    adapter_path = str(adapter_manifest.get("adapter_path") or "").strip().replace("\\", "/").rstrip("/")
    adapter_crate = str(adapter_manifest.get("adapter_crate") or "").strip()
    adapter_package_name = str(adapter_manifest.get("adapter_package_name") or "").strip()

    candidates: List[str] = []

    if adapter_path:
        candidates.extend([
            f"{adapter_path}/Cargo.toml",
            f"{adapter_path}/src/lib.rs",
            f"{adapter_path}/src/adapter/mod.rs",
            f"{adapter_path}/src/adapter/register_adapter.rs",
            f"{adapter_path}/src/adapter/device_adapter.rs",
            f"{adapter_path}/src/error.rs",
        ])
        return candidates

    if adapter_crate:
        base = f"system/software/adapter/{adapter_crate}".rstrip("/")
        candidates.extend([
            f"{base}/Cargo.toml",
            f"{base}/src/lib.rs",
            f"{base}/src/adapter/mod.rs",
            f"{base}/src/adapter/register_adapter.rs",
            f"{base}/src/adapter/device_adapter.rs",
            f"{base}/src/error.rs",
        ])
        return candidates

    if adapter_package_name:
        base = f"system/software/adapter/{adapter_package_name}".rstrip("/")
        candidates.extend([
            f"{base}/Cargo.toml",
            f"{base}/src/lib.rs",
            f"{base}/src/adapter/mod.rs",
            f"{base}/src/adapter/register_adapter.rs",
            f"{base}/src/adapter/device_adapter.rs",
            f"{base}/src/error.rs",
        ])
        return candidates

    return []

def _derive_service_required_files(services_manifest: Dict[str, Any]) -> List[str]:
    service_root = str(services_manifest.get("services_root") or "system/software/services").strip().replace("\\", "/").rstrip("/")
    files = [
        f"{service_root}/Cargo.toml",
    ]

    crate_names = services_manifest.get("service_crates") or services_manifest.get("crate_names") or []
    if isinstance(crate_names, list):
        for crate in crate_names:
            name = str(crate or "").strip()
            if name:
                files.append(f"{service_root}/{name}/Cargo.toml")
                files.append(f"{service_root}/{name}/src/lib.rs")

    return files

def _build_manifest(
    source_workflow_id: str,
    files: List[str],
    missing_required_files: List[str],
    entry: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "package_type": "system_software_package",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "artifact_count": len(files),
        "files": files,
        "missing_required_files": missing_required_files,
        "package_status": "complete" if (not missing_required_files and (entry or {}).get("entry_resolution_status") == "resolved") else "incomplete",
        "entry": entry,
    }



def _markdown_summary(manifest: Dict[str, Any], inferred_files: List[str]) -> str:
    lines = [
        "# System Software Package",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}",
        f"- Artifact count: {manifest.get('artifact_count')}",
        f"- Inferred required files added: {len(inferred_files)}",
        f"- Package status: `{manifest.get('package_status')}`",
        f"- Entry binary: `{((manifest.get('entry') or {}).get('binary_name') or '')}`",
        "",
    ]

    missing = manifest.get("missing_required_files") or []
    if missing:
        lines.extend(["## Missing required files", ""])
        for item in missing:
            lines.append(f"- `{item}`")
        lines.append("")

    lines.extend(["## Artifacts", ""])
    for item in manifest.get("files") or []:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    sdk_manifest = _load_manifest(state, workflow_dir, "system_software_sdk_manifest", "system_software_sdk_manifest_path", "system/software/sdk/system_software_sdk_manifest.json")
    app_manifest = _load_manifest(state, workflow_dir, "system_software_application_manifest", "system_software_application_manifest_path", "system/software/apps/system_software_application_manifest.json")
    tools_manifest = _load_manifest(state, workflow_dir, "system_software_tools_manifest", "system_software_tools_manifest_path", "system/software/tools/system_software_tools_manifest.json")
    build_manifest = _load_manifest(state, workflow_dir, "system_software_build_manifest", "system_software_build_manifest_path", "system/software/build/system_software_build_manifest.json")
    test_manifest = _load_manifest(state, workflow_dir, "system_software_test_manifest", "system_software_test_manifest_path", "system/software/tests/system_software_test_manifest.json")
    mock_manifest = _load_manifest(state, workflow_dir, "system_software_mock_manifest", "system_software_mock_manifest_path", "system/software/mock/system_software_mock_manifest.json")
    adapter_manifest = _load_manifest(
        state, workflow_dir,
        "system_software_adapter_manifest",
        "system_software_adapter_manifest_path",
        "system/software/adapter/system_software_adapter_manifest.json"
    )
    if not adapter_manifest:
        adapter_manifest = state.get("system_software_adapter_manifest") or {}
    services_manifest = _load_manifest(
        state, workflow_dir,
        "system_software_core_services_manifest",
        "system_software_core_services_manifest_path",
        "system/software/services/system_software_core_services_manifest.json"
    )

    if not sdk_manifest:
        state["status"] = "❌ system software sdk manifest missing"
        return state
    if not app_manifest:
        state["status"] = "❌ system software application manifest missing"
        return state

    files = _collect_files(
        sdk_manifest,
        app_manifest,
        tools_manifest,
        build_manifest,
        test_manifest,
        mock_manifest,
        adapter_manifest,
        services_manifest
    )

    seen = set(files)
    inferred_files: List[str] = []

    for path in _derive_adapter_required_files(adapter_manifest):
        p = str(path).strip().replace("\\", "/")
        if p and p not in seen:
            inferred_files.append(p)
    for path in _derive_service_required_files(services_manifest):
        p = str(path).strip().replace("\\", "/")
        if p and p not in seen:
            inferred_files.append(p)

    
    _extend_if_present(files, seen, *inferred_files)

    missing_required_files: List[str] = []
    if workflow_dir:
        for path in inferred_files:
            if not _file_exists(workflow_dir, path):
                missing_required_files.append(path)

    entry = _derive_entry_metadata(sdk_manifest, app_manifest, build_manifest)

    source_workflow_id = str(app_manifest.get("source_workflow_id") or sdk_manifest.get("source_workflow_id") or "")
    manifest = _build_manifest(source_workflow_id, files, missing_required_files, entry)

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest, inferred_files))
    _record_text(workflow_id, FILELIST_TXT, ("\n".join(files) + "\n") if files else "")
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "artifact_count": len(files),
        "inferred_required_files_added": inferred_files,
        "missing_required_files": missing_required_files,
        "entry": entry,
        "included_manifests": {
            "sdk": bool(sdk_manifest),
            "apps": bool(app_manifest),
            "tools": bool(tools_manifest),
            "build": bool(build_manifest),
            "tests": bool(test_manifest),
            "mock": bool(mock_manifest),
            "adapter": bool(adapter_manifest),
            "services": bool(services_manifest),
        }
    }, indent=2))
    state["system_software_package"] = manifest
    state["system_software_package_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["system_software_entry"] = manifest.get("entry") or {}
    state["system_software_entry_command"] = ((manifest.get("entry") or {}).get("command") or [])
    state["package_status"] = manifest.get("package_status") or "unknown"
    state["status"] = "✅ System software package generated" if manifest.get("package_status") == "complete" else "⚠️ System software package incomplete"


    return state
