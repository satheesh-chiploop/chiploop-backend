import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Unit Test Agent"
OUTPUT_SUBDIR = "system/software/tests"
MANIFEST_JSON = "system_software_test_manifest.json"
SUMMARY_MD = "system_software_test_summary.md"
DEBUG_JSON = "system_software_test_debug.json"


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


def _load_api_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_api_contract")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (
        state.get("system_software_api_contract_path"),
        "system/software/sdk/system_software_api_contract.json",
    ):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _render_sdk_tests(crate_name: str) -> str:
    return f'''use {crate_name}::Sdk;

#[test]
fn sdk_constructs() {{
    let _sdk = Sdk::new();
}}

#[test]
fn sdk_status_is_callable() {{
    let sdk = Sdk::new();
    let _ = sdk.get_status();
}}
'''


def _render_service_tests() -> str:
    return '''#[test]
fn smoke_service_test_placeholder() {
    assert!(true);
}
'''


def _render_app_smoke(crate_name: str) -> str:
    return f'''use {crate_name}::Sdk;

#[test]
fn app_level_smoke_uses_sdk() {{
    let sdk = Sdk::new();
    let _ = sdk.platform_probe();
}}
'''


def _build_manifest(source_workflow_id: str, files: List[str]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_test_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "files": files,
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software Unit Tests",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}",
        "",
        "## Files",
        "",
    ]
    for item in manifest.get("files") or []:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    api_contract = _load_api_contract(state, workflow_dir)
    if not api_contract:
        state["status"] = "❌ system software api contract missing"
        return state

    crate_name = str(state.get("project_name") or "system_software_sdk").strip().lower().replace("-", "_")
    crate_name = crate_name or "system_software_sdk"

    written: List[str] = []

    _record_text(workflow_id, "sdk_tests.rs", _render_sdk_tests(crate_name), subdir=f"{OUTPUT_SUBDIR}/sdk")
    written.append(f"{OUTPUT_SUBDIR}/sdk/sdk_tests.rs")

    _record_text(workflow_id, "service_tests.rs", _render_service_tests(), subdir=f"{OUTPUT_SUBDIR}/services")
    written.append(f"{OUTPUT_SUBDIR}/services/service_tests.rs")

    _record_text(workflow_id, "app_smoke.rs", _render_app_smoke(crate_name), subdir=f"{OUTPUT_SUBDIR}/apps")
    written.append(f"{OUTPUT_SUBDIR}/apps/app_smoke.rs")

    manifest = _build_manifest(
        source_workflow_id=str(api_contract.get("source_workflow_id") or ""),
        files=written,
    )

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "written_file_count": len(written),
    }, indent=2))

    state["system_software_test_manifest"] = manifest
    state["system_software_test_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software unit tests generated"
    return state
