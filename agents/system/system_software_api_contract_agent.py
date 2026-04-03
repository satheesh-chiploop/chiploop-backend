import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software API Contract Agent"
OUTPUT_SUBDIR = "system/software/sdk"
API_JSON = "system_software_api_contract.json"
API_MD = "system_software_api_contract.md"


def _now() -> str:
    return datetime.datetime.now().isoformat()


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
    model = state.get("system_software_capability_model")
    if isinstance(model, dict):
        return model
    for candidate in (
        state.get("system_software_capability_model_path"),
        "system/software/model/system_software_capability_model.json",
    ):
        if isinstance(candidate, str) and candidate.strip():
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj
    return {}


def _service_entry(name: str, methods: List[Dict[str, Any]], description: str) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "methods": methods,
    }


def _build_api_contract(model: Dict[str, Any]) -> Dict[str, Any]:
    services = model.get("software_services") or {}
    register_summary = ((model.get("hardware_model") or {}).get("register_summary")) or {}
    api_groups: List[Dict[str, Any]] = []

    if services.get("register_access"):
        api_groups.append(
            _service_entry(
                "register_access",
                [
                    {"name": "read_register", "inputs": ["register_name"], "returns": "u32|u64"},
                    {"name": "write_register", "inputs": ["register_name", "value"], "returns": "status"},
                ],
                "Public register access wrapper over the firmware HAL/driver layer.",
            )
        )

    if services.get("driver_layer"):
        api_groups.append(
            _service_entry(
                "device_driver",
                [
                    {"name": "device_init", "inputs": ["config"], "returns": "status"},
                    {"name": "device_shutdown", "inputs": [], "returns": "status"},
                    {"name": "get_status", "inputs": [], "returns": "device_status"},
                ],
                "Primary software-facing driver contract for device lifecycle and status.",
            )
        )

    if services.get("interrupt_control"):
        api_groups.append(
            _service_entry(
                "interrupts",
                [
                    {"name": "register_interrupt_handler", "inputs": ["irq_name", "callback"], "returns": "status"},
                    {"name": "enable_interrupt", "inputs": ["irq_name"], "returns": "status"},
                ],
                "Optional interrupt control/service layer.",
            )
        )

    if services.get("dma_control"):
        api_groups.append(
            _service_entry(
                "dma",
                [
                    {"name": "dma_submit", "inputs": ["descriptor"], "returns": "status"},
                    {"name": "dma_poll", "inputs": ["channel"], "returns": "dma_status"},
                ],
                "Optional DMA submission and polling services.",
            )
        )

    management_methods: List[Dict[str, Any]] = []
    if services.get("clock_control"):
        management_methods.append({"name": "set_clock_mode", "inputs": ["mode"], "returns": "status"})
    if services.get("reset_control"):
        management_methods.append({"name": "request_reset", "inputs": ["domain"], "returns": "status"})
    if services.get("power_mode_control"):
        management_methods.append({"name": "set_power_mode", "inputs": ["mode"], "returns": "status"})
    if services.get("boot_orchestration"):
        management_methods.append({"name": "query_boot_state", "inputs": [], "returns": "boot_state"})
    if management_methods:
        api_groups.append(
            _service_entry(
                "platform_management",
                management_methods,
                "Platform control services for clocks, reset, power, and boot state.",
            )
        )

    if not api_groups:
        api_groups.append(
            _service_entry(
                "platform",
                [{"name": "platform_probe", "inputs": [], "returns": "platform_info"}],
                "Minimal software-facing platform probe when no richer services were discovered.",
            )
        )

    return {
        "package_type": "system_software_api_contract",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": model.get("source_workflow_id"),
        "platform_identity": model.get("platform_identity"),
        "api_style": {
            "public_api_language": "c_or_rust_adapter",
            "error_model": "status_return_values",
            "config_model": "struct_or_json_config",
        },
        "register_model_summary": {
            "register_count": register_summary.get("register_count"),
            "bus": register_summary.get("bus"),
            "addr_width": register_summary.get("addr_width"),
            "data_width": register_summary.get("data_width"),
        },
        "public_api_groups": api_groups,
        "internal_api_groups": [
            {
                "name": "runtime_validation_internal",
                "description": "Internal-only hooks for simulation/runtime validation; not intended for application developers.",
                "methods": [
                    {"name": "attach_runtime_backend", "inputs": ["backend"], "returns": "status"},
                    {"name": "inject_test_hook", "inputs": ["hook_name"], "returns": "status"},
                ],
            }
        ],
        "constraints": model.get("software_constraints") or {},
    }


def _markdown_report(contract: Dict[str, Any]) -> str:
    lines = [
        "# System Software API Contract",
        "",
        f"- Generated at: {contract.get('generated_at')}",
        f"- Source workflow id: {contract.get('source_workflow_id') or 'unavailable'}",
        "",
        "## Public API groups",
        "",
    ]
    for group in contract.get("public_api_groups") or []:
        lines.append(f"### {group.get('name')}")
        lines.append(group.get("description") or "")
        for method in group.get("methods") or []:
            lines.append(f"- `{method.get('name')}`({', '.join(method.get('inputs') or [])}) -> `{method.get('returns')}`")
        lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir")
    print(f"\n🧩 Running {AGENT_NAME}")

    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for API contract"
        return state

    model = _load_model(state, os.path.abspath(workflow_dir))
    if not model:
        state["status"] = "❌ capability model missing"
        return state

    contract = _build_api_contract(model)
    _record_text(workflow_id, API_JSON, json.dumps(contract, indent=2))
    _record_text(workflow_id, API_MD, _markdown_report(contract))

    state["system_software_api_contract"] = contract
    state["system_software_api_contract_path"] = f"{OUTPUT_SUBDIR}/{API_JSON}"
    state["status"] = "✅ System software API contract generated"
    return state
