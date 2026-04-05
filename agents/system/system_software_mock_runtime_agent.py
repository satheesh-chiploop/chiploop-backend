import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Mock Runtime Agent"
OUTPUT_SUBDIR = "system/software/mock"
MANIFEST_JSON = "system_software_mock_manifest.json"
SUMMARY_MD = "system_software_mock_summary.md"
DEBUG_JSON = "system_software_mock_debug.json"


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


def _load_input_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_input_contract")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (
        state.get("system_software_input_contract_path"),
        "system/software/input/system_software_input_contract.json",
    ):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, str(candidate)))
            if loaded:
                return loaded
    return {}


def _render_mock_runtime() -> str:
    return '''use std::collections::HashMap;

#[derive(Debug, Default, Clone)]
pub struct MockRuntime {
    registers: HashMap<String, u32>,
    power_mode: String,
}

impl MockRuntime {
    pub fn new() -> Self {
        Self {
            registers: HashMap::new(),
            power_mode: "active".into(),
        }
    }

    pub fn read_register(&self, register_name: &str) -> u32 {
        *self.registers.get(register_name).unwrap_or(&0)
    }

    pub fn write_register(&mut self, register_name: &str, value: u32) {
        self.registers.insert(register_name.to_string(), value);
    }

    pub fn set_power_mode(&mut self, mode: &str) {
        self.power_mode = mode.to_string();
    }

    pub fn power_mode(&self) -> &str {
        &self.power_mode
    }
}
'''


def _build_manifest(source_workflow_id: str, files: List[str]) -> Dict[str, Any]:
    return {
        "package_type": "system_software_mock_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "files": files,
    }


def _markdown_summary(manifest: Dict[str, Any]) -> str:
    lines = [
        "# System Software Mock Runtime",
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

    contract = _load_input_contract(state, workflow_dir)
    if not contract:
        state["status"] = "❌ system software input contract missing"
        return state

    written: List[str] = []
    _record_text(workflow_id, "mock_runtime.rs", _render_mock_runtime(), subdir=f"{OUTPUT_SUBDIR}/src")
    written.append(f"{OUTPUT_SUBDIR}/src/mock_runtime.rs")

    manifest = _build_manifest(
        source_workflow_id=str(contract.get("source_workflow_id") or ""),
        files=written,
    )

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(manifest))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "written_file_count": len(written),
    }, indent=2))

    state["system_software_mock_manifest"] = manifest
    state["system_software_mock_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["status"] = "✅ System software mock runtime generated"
    return state
