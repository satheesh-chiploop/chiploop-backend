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
            p = str(path).strip()
            if p and p not in seen:
                seen.add(p)
                files.append(p)
    return files


def _build_manifest(source_workflow_id: str, files: List[str]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_package",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "artifact_count": len(files),
        "files": files,
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software Package",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}",
        f"- Artifact count: {manifest.get('artifact_count')}",
        "",
        "## Artifacts",
        "",
    ]
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
    source_workflow_id = str(app_manifest.get("source_workflow_id") or sdk_manifest.get("source_workflow_id") or "")

    manifest = _build_manifest(source_workflow_id, files)

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, FILELIST_TXT, ("\n".join(files) + "\n") if files else "")
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "artifact_count": len(files),
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
    state["status"] = "✅ System software package generated"
    return state
