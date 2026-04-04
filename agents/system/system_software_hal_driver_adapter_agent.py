
import datetime
import json
import os
from typing import Any, Dict, List, Optional
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software HAL/Driver Adapter Agent"
OUTPUT_SUBDIR = "system/software/adapter"
MANIFEST_JSON = "system_software_adapter_manifest.json"
SUMMARY_MD = "system_software_adapter_summary.md"
DEBUG_JSON = "system_software_adapter_debug.json"

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(workflow_id=workflow_id, agent_name=AGENT_NAME, subdir=subdir, filename=filename, content=content)
    except Exception:
        return None

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

def _load_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_input_contract")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_input_contract_path"), "system/software/input/system_software_input_contract.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _load_api_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_api_contract")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_api_contract_path"), "system/software/sdk/system_software_api_contract.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _crate_name(value: str) -> str:
    text = (value or "system_software_sdk").strip().lower().replace("-", "_")
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    crate = "".join(out).strip("_") or "system_software_sdk"
    if crate[0].isdigit():
        crate = f"sdk_{crate}"
    return crate

def _render_register_adapter() -> str:
    return '''use crate::error::SoftwareError;

pub trait RegisterIo {
    fn read_u32(&self, register_name: &str) -> Result<u32, SoftwareError>;
    fn write_u32(&self, register_name: &str, value: u32) -> Result<(), SoftwareError>;
}

#[derive(Debug, Default, Clone)]
pub struct RegisterAdapter;

impl RegisterAdapter {
    pub fn new() -> Self {
        Self {}
    }
}

impl RegisterIo for RegisterAdapter {
    fn read_u32(&self, register_name: &str) -> Result<u32, SoftwareError> {
        Err(SoftwareError::Unimplemented(format!("read_u32:{register_name}")))
    }

    fn write_u32(&self, register_name: &str, value: u32) -> Result<(), SoftwareError> {
        let _ = value;
        Err(SoftwareError::Unimplemented(format!("write_u32:{register_name}")))
    }
}
'''

def _render_device_adapter(enable_interrupts: bool, enable_dma: bool, enable_power: bool) -> str:
    extra = []
    if enable_interrupts:
        extra.append('''    pub fn enable_interrupt(&self, irq_name: &str) -> Result<(), SoftwareError> {
        Err(SoftwareError::Unimplemented(format!("enable_interrupt:{irq_name}")))
    }

''')
    if enable_dma:
        extra.append('''    pub fn submit_dma(&self, channel: &str) -> Result<String, SoftwareError> {
        Err(SoftwareError::Unimplemented(format!("submit_dma:{channel}")))
    }

''')
    if enable_power:
        extra.append('''    pub fn set_power_mode(&self, mode: &str) -> Result<(), SoftwareError> {
        Err(SoftwareError::Unimplemented(format!("set_power_mode:{mode}")))
    }

''')
    return f'''use crate::error::SoftwareError;

#[derive(Debug, Default, Clone)]
pub struct DeviceAdapter;

impl DeviceAdapter {{
    pub fn new() -> Self {{
        Self {{}}
    }}

    pub fn device_init(&self, profile: &str) -> Result<(), SoftwareError> {{
        Err(SoftwareError::Unimplemented(format!("device_init:{{profile}}")))
    }}

    pub fn device_shutdown(&self) -> Result<(), SoftwareError> {{
        Err(SoftwareError::Unimplemented("device_shutdown".into()))
    }}

    pub fn get_status(&self) -> Result<String, SoftwareError> {{
        Ok("healthy".into())
    }}

{''.join(extra)}}}
'''

def _render_error_module() -> str:
    return '''#[derive(Debug, thiserror::Error)]
pub enum SoftwareError {
    #[error("unimplemented: {0}")]
    Unimplemented(String),
}
'''

def _manifest(contract: Dict[str, Any], api_contract: Dict[str, Any]) -> Dict[str, Any]:
    firmware_contract = contract.get("firmware_contract") or {}
    return {
        "package_type": "system_software_adapter_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": contract.get("source_workflow_id"),
        "adapter_contract": {
            "hal_path": firmware_contract.get("hal_path") or "",
            "driver_path": firmware_contract.get("driver_path") or "",
            "register_map_path": firmware_contract.get("register_map_path") or "",
            "interrupt_mapping_path": firmware_contract.get("interrupt_mapping_path") or "",
            "dma_integration_path": firmware_contract.get("dma_integration_path") or "",
        },
        "api_group_names": [str((g or {}).get("name")) for g in (api_contract.get("public_api_groups") or []) if isinstance(g, dict)],
        "stability_contract": {
            "runtime_binding_model": "adapter_traits_and_impl_stubs",
            "register_access_model": "named_register_access",
            "driver_lifecycle_model": "device_init/device_shutdown/get_status",
        },
    }

def _markdown_summary(manifest: Dict[str, Any], files: List[str]) -> str:
    lines = ["# System Software HAL/Driver Adapter", "", f"- Generated at: {manifest.get('generated_at')}", f"- Source workflow id: {manifest.get('source_workflow_id') or 'unavailable'}", "", "## Adapter contract", ""]
    for key, value in (manifest.get("adapter_contract") or {}).items():
        lines.append(f"- `{key}` → `{value or 'unavailable'}`")
    lines.extend(["", "## Generated files", ""])
    for item in files:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n🔌 Running {AGENT_NAME}")

    contract = _load_contract(state, workflow_dir)
    if not contract:
        state["status"] = "❌ system software input contract missing"
        return state
    api_contract = _load_api_contract(state, workflow_dir)
    if not api_contract:
        state["status"] = "❌ system software API contract missing"
        return state

    crate_name = _crate_name(state.get("project_name") or "system_software_sdk")
    fc = contract.get("firmware_contract") or {}
    written_files = []
    files_to_write = {
        f"{crate_name}/src/adapter/register_adapter.rs": _render_register_adapter(),
        f"{crate_name}/src/adapter/device_adapter.rs": _render_device_adapter(bool(fc.get("interrupt_mapping_path")), bool(fc.get("dma_integration_path")), bool(fc.get("power_mode_path"))),
        f"{crate_name}/src/error.rs": _render_error_module(),
    }
    for rel_path, content in files_to_write.items():
        subdir, filename = rel_path.rsplit("/", 1)
        _record_text(workflow_id, filename, content, subdir=f"{OUTPUT_SUBDIR}/{subdir}")
        written_files.append(f"{OUTPUT_SUBDIR}/{rel_path}")

    manifest = _manifest(contract, api_contract)
    debug_payload = {"agent": AGENT_NAME, "generated_at": _now(), "written_file_count": len(written_files), "contract_paths": manifest.get("adapter_contract") or {}}
    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest, written_files))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    # NEW FILES
    _record_text(workflow_id, "Cargo.toml", f"""
    [package]
    name = "{crate_name}_adapter"
    version = "0.1.0"
    edition = "2021"

    [dependencies]
    thiserror = "1"
    """, subdir=f"{OUTPUT_SUBDIR}/{crate_name}")

    _record_text(workflow_id, "lib.rs", """
    pub mod adapter;
    pub mod error;

    pub use adapter::{RegisterAdapter, DeviceAdapter};
    """, subdir=f"{OUTPUT_SUBDIR}/{crate_name}/src")


    _record_text(workflow_id, "mod.rs", """
    pub mod register_adapter;
    pub mod device_adapter;

    pub use register_adapter::RegisterAdapter;
    pub use device_adapter::DeviceAdapter;
    """, subdir=f"{OUTPUT_SUBDIR}/{crate_name}/src/adapter")


    state["system_software_adapter_manifest"] = manifest
    state["system_software_adapter_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software HAL/driver adapter generated"
    return state
