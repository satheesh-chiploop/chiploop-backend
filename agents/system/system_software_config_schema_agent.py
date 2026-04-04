
import datetime
import json
import os
from typing import Any, Dict, List, Optional
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Config Schema Agent"
OUTPUT_SUBDIR = "system/software/config"
CONFIG_JSON = "system_software_config_schema.json"
SUMMARY_MD = "system_software_config_schema.md"
DEBUG_JSON = "system_software_config_schema_debug.json"

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

def _schema(model: Dict[str, Any]) -> Dict[str, Any]:
    services = model.get("software_services") or {}
    return {
        "package_type": "system_software_config_schema",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": model.get("source_workflow_id"),
        "schema_version": "1.0",
        "config_format": "json_or_toml",
        "properties": {
            "system.profile": {"type": "string", "default": "default"},
            "system.log_level": {"type": "string", "default": "info"},
            "runtime.enable_health_monitor": {"type": "boolean", "default": True},
            "runtime.enable_telemetry": {"type": "boolean", "default": True},
            "runtime.poll_interval_ms": {"type": "integer", "default": 1000, "minimum": 10},
            "device.init_profile": {"type": "string", "default": "nominal"},
            "device.startup_timeout_ms": {"type": "integer", "default": 5000, "minimum": 100},
        },
        "feature_toggles": {
            "interrupt_control": bool(services.get("interrupt_control")),
            "dma_control": bool(services.get("dma_control")),
            "power_mode_control": bool(services.get("power_mode_control")),
            "clock_control": bool(services.get("clock_control")),
            "reset_control": bool(services.get("reset_control")),
        },
        "required_sections": ["system", "runtime", "device"],
        "recommended_sections": ["telemetry", "diagnostics", "control"],
    }

def _default_json(schema: Dict[str, Any]) -> str:
    p = schema.get("properties") or {}
    payload = {
        "system": {"profile": p.get("system.profile", {}).get("default"), "log_level": p.get("system.log_level", {}).get("default")},
        "runtime": {"enable_health_monitor": p.get("runtime.enable_health_monitor", {}).get("default"), "enable_telemetry": p.get("runtime.enable_telemetry", {}).get("default"), "poll_interval_ms": p.get("runtime.poll_interval_ms", {}).get("default")},
        "device": {"init_profile": p.get("device.init_profile", {}).get("default"), "startup_timeout_ms": p.get("device.startup_timeout_ms", {}).get("default")},
    }
    return json.dumps(payload, indent=2)

def _default_toml(schema: Dict[str, Any]) -> str:
    p = schema.get("properties") or {}
    return f'''[system]
profile = "{p.get("system.profile", {}).get("default", "default")}"
log_level = "{p.get("system.log_level", {}).get("default", "info")}"

[runtime]
enable_health_monitor = {str(p.get("runtime.enable_health_monitor", {}).get("default", True)).lower()}
enable_telemetry = {str(p.get("runtime.enable_telemetry", {}).get("default", True)).lower()}
poll_interval_ms = {p.get("runtime.poll_interval_ms", {}).get("default", 1000)}

[device]
init_profile = "{p.get("device.init_profile", {}).get("default", "nominal")}"
startup_timeout_ms = {p.get("device.startup_timeout_ms", {}).get("default", 5000)}
'''

def _markdown(schema: Dict[str, Any], files: List[str]) -> str:
    lines = ["# System Software Config Schema", "", f"- Generated at: {schema.get('generated_at')}", f"- Source workflow id: {schema.get('source_workflow_id') or 'unavailable'}", "", "## Feature toggles", ""]
    for key, value in (schema.get("feature_toggles") or {}).items():
        lines.append(f"- `{key}` → `{value}`")
    lines.extend(["", "## Generated files", ""])
    for item in files:
        lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)

def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""
    print(f"\n⚙️ Running {AGENT_NAME}")
    model = _load_model(state, workflow_dir)
    if not model:
        state["status"] = "❌ system software capability model missing"
        return state

    schema = _schema(model)
    written = []
    for filename, content in {"default_config.json": json.dumps(schema, indent=2), "default_runtime_config.json": _default_json(schema), "default_runtime_config.toml": _default_toml(schema)}.items():
        _record_text(workflow_id, filename, content)
        written.append(f"{OUTPUT_SUBDIR}/{filename}")

    _record_text(workflow_id, CONFIG_JSON, json.dumps(schema, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown(schema, written))
    _record_text(workflow_id, DEBUG_JSON, json.dumps({"agent": AGENT_NAME, "generated_at": _now(), "feature_toggles": schema.get("feature_toggles") or {}}, indent=2))
    state["system_software_config_schema"] = schema
    state["system_software_config_schema_path"] = f"{OUTPUT_SUBDIR}/{CONFIG_JSON}"
    state["status"] = "✅ System software config schema generated"
    return state
