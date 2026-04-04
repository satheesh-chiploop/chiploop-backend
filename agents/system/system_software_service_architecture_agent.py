
import datetime
import json
import os
from typing import Any, Dict, Optional
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Service Architecture Agent"
OUTPUT_SUBDIR = "system/software/architecture"
ARCH_JSON = "system_software_service_architecture.json"
SUMMARY_MD = "system_software_service_architecture.md"
DEBUG_JSON = "system_software_service_architecture_debug.json"

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(workflow_id=workflow_id, agent_name=AGENT_NAME, subdir=OUTPUT_SUBDIR, filename=filename, content=content)
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

def _load_model(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_capability_model")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_capability_model_path"), "system/software/model/system_software_capability_model.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _load_config(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_software_config_schema")
    if isinstance(obj, dict) and obj:
        return obj
    for candidate in (state.get("system_software_config_schema_path"), "system/software/config/system_software_config_schema.json"):
        if _is_nonempty_str(candidate) and workflow_dir:
            loaded = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if loaded:
                return loaded
    return {}

def _build(model: Dict[str, Any], config_schema: Dict[str, Any]) -> Dict[str, Any]:
    services = model.get("software_services") or {}
    nodes = [
        {"name": "sdk", "kind": "library", "responsibility": "developer_facing_api_layer"},
        {"name": "adapter", "kind": "library", "responsibility": "firmware_to_software_bridge"},
        {"name": "config_runtime", "kind": "library", "responsibility": "config_loading_and_validation"},
        {"name": "init_manager", "kind": "service", "responsibility": "startup_order_and_readiness"},
        {"name": "health_monitor", "kind": "service", "responsibility": "health_status_and_fault_summarization"},
        {"name": "control_service", "kind": "service", "responsibility": "platform_control_surface"},
        {"name": "diag_cli", "kind": "tool", "responsibility": "operator_diagnostics"},
    ]
    if services.get("dma_control"):
        nodes.append({"name": "dma_service", "kind": "service", "responsibility": "dma_submission_and_status"})
    if services.get("interrupt_control"):
        nodes.append({"name": "interrupt_service", "kind": "service", "responsibility": "interrupt_registration_and_dispatch"})
    if services.get("power_mode_control"):
        nodes.append({"name": "power_service", "kind": "service", "responsibility": "power_mode_management"})
    edges = [
        {"from": "sdk", "to": "adapter", "reason": "sdk uses stable adapter layer"},
        {"from": "sdk", "to": "config_runtime", "reason": "sdk consumes runtime config"},
        {"from": "init_manager", "to": "adapter", "reason": "startup uses firmware bridge"},
        {"from": "health_monitor", "to": "adapter", "reason": "health polling reads device status"},
        {"from": "control_service", "to": "adapter", "reason": "control operations dispatch through adapter"},
        {"from": "diag_cli", "to": "sdk", "reason": "tooling should call public sdk"},
    ]
    return {
        "package_type": "system_software_service_architecture",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": model.get("source_workflow_id"),
        "architecture_style": "layered_services_and_tools",
        "required_sections": config_schema.get("required_sections") or [],
        "nodes": nodes,
        "edges": edges,
        "deployment_notes": {
            "runtime_validation_in_separate_workflow": True,
            "sdk_reusable_across_apps": True,
            "services_should_depend_on_adapter_not_raw_firmware": True,
        },
    }

def _markdown(arch: Dict[str, Any]) -> str:
    lines = ["# System Software Service Architecture", "", f"- Generated at: {arch.get('generated_at')}", f"- Source workflow id: {arch.get('source_workflow_id') or 'unavailable'}", "", "## Nodes", ""]
    for node in arch.get("nodes") or []:
        lines.append(f"- `{node.get('name')}` ({node.get('kind')}) → `{node.get('responsibility')}`")
    lines.extend(["", "## Edges", ""])
    for edge in arch.get("edges") or []:
        lines.append(f"- `{edge.get('from')}` -> `{edge.get('to')}` : {edge.get('reason')}")
    lines.append("")
    return "\n".join(lines)

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n🏗️ Running {AGENT_NAME}")
    model = _load_model(state, workflow_dir)
    if not model:
        state["status"] = "❌ system software capability model missing"
        return state
    config_schema = _load_config(state, workflow_dir)
    if not config_schema:
        state["status"] = "❌ system software config schema missing"
        return state

    arch = _build(model, config_schema)
    _record_text(workflow_id, ARCH_JSON, json.dumps(arch, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown(arch))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({"agent": AGENT_NAME, "generated_at": _now(), "node_count": len(arch.get("nodes") or []), "edge_count": len(arch.get("edges") or [])}, indent=2))
    state["system_software_service_architecture"] = arch
    state["system_software_service_architecture_path"] = f"{OUTPUT_SUBDIR}/{ARCH_JSON}"
    state["status"] = "✅ System software service architecture generated"
    return state
