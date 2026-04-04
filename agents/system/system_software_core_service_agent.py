
import datetime
import json
import os
from typing import Any, Dict, List, Optional
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Core Service Agent"
OUTPUT_SUBDIR = "system/software/services"
MANIFEST_JSON = "system_software_core_services_manifest.json"
SUMMARY_MD = "system_software_core_services_summary.md"
DEBUG_JSON = "system_software_core_services_debug.json"

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(workflow_id=workflow_id, agent_name=AGENT_NAME, subdir=subdir, filename=filename, content=content)
    except Exception:
        return None

def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

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

def _load_arch(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_service_architecture")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_service_architecture_path"), "system/software/architecture/system_software_service_architecture.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _render_service(name: str, responsibility: str) -> str:
    struct_name = "".join([part.capitalize() for part in name.split("_")]) or "Service"
    return f'''use crate::error::SoftwareError;

#[derive(Debug, Default, Clone)]
pub struct {struct_name};

impl {struct_name} {{
    pub fn new() -> Self {{
        Self {{}}
    }}

    pub fn responsibility(&self) -> &'static str {{
        "{responsibility}"
    }}

    pub fn start(&self) -> Result<(), SoftwareError> {{
        Ok(())
    }}

    pub fn stop(&self) -> Result<(), SoftwareError> {{
        Ok(())
    }}

    pub fn poll(&self) -> Result<String, SoftwareError> {{
        Ok("ok".into())
    }}
}}
'''

def _render_mod(names: List[str]) -> str:
    return "\n".join([f"pub mod {name};" for name in names]) + "\n"

def _render_error() -> str:
    return '''#[derive(Debug, thiserror::Error)]
pub enum SoftwareError {
    #[error("service error: {0}")]
    Service(String),
}
'''

def _manifest(arch: Dict[str, Any], files: List[str], names: List[str]) -> Dict[str, Any]:
    return {"package_type": "system_software_core_services_manifest", "package_version": "1.0", "generated_at": _now(), "source_workflow_id": arch.get("source_workflow_id"), "service_names": names, "service_count": len(names), "files": files}

def _markdown(manifest: Dict[str, Any]) -> str:
    lines = ["# System Software Core Services", "", f"- Generated at: {manifest.get('generated_at')}", f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}", f"- Service count: {manifest.get('service_count')}", "", "## Services", ""]
    for name in manifest.get("service_names") or []:
        lines.append(f"- `{name}`")
    lines.extend(["", "## Files", ""])
    for item in manifest.get("files") or []:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n🧱 Running {AGENT_NAME}")
    arch = _load_arch(state, workflow_dir)
    if not arch:
        state["status"] = "❌ system software architecture missing"
        return state

    nodes = [n for n in (arch.get("nodes") or []) if (n or {}).get("kind") == "service"]
    if not nodes:
        state["status"] = "❌ no service nodes found in architecture"
        return state

    written, names = [], []
    for node in nodes:
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        names.append(name)
        _record_text(workflow_id, f"{name}.rs", _render_service(name, str(node.get("responsibility") or "service_runtime")), subdir=f"{OUTPUT_SUBDIR}/src")
        written.append(f"{OUTPUT_SUBDIR}/src/{name}.rs")

    _record_text(workflow_id, "error.rs", _render_error(), subdir=f"{OUTPUT_SUBDIR}/src")
    written.extend([f"{OUTPUT_SUBDIR}/src/mod.rs", f"{OUTPUT_SUBDIR}/src/error.rs"])

    manifest = _manifest(arch, written, names)
    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({"agent": AGENT_NAME, "generated_at": _now(), "service_names": names}, indent=2))


    _record_text(workflow_id, "Cargo.toml", """
[package]
name = "system_services"
version = "0.1.0"
edition = "2021"

[dependencies]
thiserror = "1"
""", subdir=OUTPUT_SUBDIR)

    _record_text(workflow_id, "lib.rs", f"""
pub mod error;
{''.join([f'pub mod {name};\n' for name in names])}
""", subdir=f"{OUTPUT_SUBDIR}/src")
    state["system_software_core_services_manifest"] = manifest
    state["system_software_core_services_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software core services generated"
    return state
