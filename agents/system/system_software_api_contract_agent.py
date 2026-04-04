import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software API Contract Agent"
OUTPUT_SUBDIR = "system/software/sdk"
API_JSON = "system_software_api_contract.json"
API_MD = "system_software_api_contract.md"
DEBUG_JSON = "system_software_api_contract_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
    if isinstance(model, dict) and model:
        return model
    for candidate in (
        state.get("system_software_capability_model_path"),
        "system/software/model/system_software_capability_model.json",
    ):
        if isinstance(candidate, str) and candidate.strip() and workflow_dir:
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj
    return {}


def _service_entry(name: str, methods: List[Dict[str, Any]], description: str, visibility: str = "public") -> Dict[str, Any]:
    return {
        "name": name,
        "visibility": visibility,
        "description": description,
        "methods": methods,
    }


def _lang_pref(state: Dict[str, Any], model: Dict[str, Any]) -> str:
    pref = state.get("target_language") or ((model.get("software_preferences") or {}).get("target_language")) or "rust"
    return str(pref).strip().lower()


def _api_style_for_lang(target_language: str, sdk_style: str) -> Dict[str, Any]:
    if target_language == "rust":
        return {
            "public_api_language": "rust",
            "error_model": "Result<T, SoftwareError>",
            "config_model": "serde_structs_and_toml_or_json",
            "package_style": sdk_style or "rust_crate",
        }
    return {
        "public_api_language": "c_or_rust_adapter",
        "error_model": "status_return_values",
        "config_model": "struct_or_json_config",
        "package_style": sdk_style or "mixed",
    }


def _register_value_type(register_summary: Dict[str, Any], target_language: str) -> str:
    data_width = register_summary.get("data_width")
    if target_language != "rust":
        return "u32|u64"
    if isinstance(data_width, int) and data_width <= 8:
        return "u8"
    if isinstance(data_width, int) and data_width <= 16:
        return "u16"
    if isinstance(data_width, int) and data_width <= 32:
        return "u32"
    return "u64"


def _build_api_contract(model: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    services = model.get("software_services") or {}
    register_summary = ((model.get("hardware_model") or {}).get("register_summary")) or {}
    target_language = _lang_pref(state, model)
    sdk_style = (state.get("sdk_style") or (model.get("software_preferences") or {}).get("sdk_style") or "rust_crate")
    reg_value_type = _register_value_type(register_summary, target_language)
    api_groups: List[Dict[str, Any]] = []

    if services.get("register_access"):
        api_groups.append(
            _service_entry(
                "register_access",
                [
                    {"name": "read_register", "inputs": ["register_name: &str"], "returns": reg_value_type},
                    {"name": "write_register", "inputs": [f"register_name: &str", f"value: {reg_value_type}"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                ],
                "Public register access wrapper over the firmware HAL/driver layer.",
            )
        )

    if services.get("driver_layer"):
        api_groups.append(
            _service_entry(
                "device_driver",
                [
                    {"name": "device_init", "inputs": ["config: DeviceConfig"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                    {"name": "device_shutdown", "inputs": [], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                    {"name": "get_status", "inputs": [], "returns": "DeviceStatus"},
                ],
                "Primary software-facing driver contract for device lifecycle and status.",
            )
        )

    if services.get("interrupt_control"):
        api_groups.append(
            _service_entry(
                "interrupts",
                [
                    {"name": "register_interrupt_handler", "inputs": ["irq_name: &str", "callback: InterruptHandler"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                    {"name": "enable_interrupt", "inputs": ["irq_name: &str"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                ],
                "Optional interrupt control/service layer.",
            )
        )

    if services.get("dma_control"):
        api_groups.append(
            _service_entry(
                "dma",
                [
                    {"name": "dma_submit", "inputs": ["descriptor: DmaDescriptor"], "returns": "Result<DmaToken, SoftwareError>" if target_language == "rust" else "status"},
                    {"name": "dma_poll", "inputs": ["channel: &str"], "returns": "DmaStatus"},
                ],
                "Optional DMA submission and polling services.",
            )
        )

    management_methods: List[Dict[str, Any]] = []
    if services.get("clock_control"):
        management_methods.append({"name": "set_clock_mode", "inputs": ["mode: ClockMode"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"})
    if services.get("reset_control"):
        management_methods.append({"name": "request_reset", "inputs": ["domain: &str"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"})
    if services.get("power_mode_control"):
        management_methods.append({"name": "set_power_mode", "inputs": ["mode: PowerMode"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"})
    if services.get("boot_orchestration"):
        management_methods.append({"name": "query_boot_state", "inputs": [], "returns": "BootState"})
    if management_methods:
        api_groups.append(
            _service_entry(
                "platform_management",
                management_methods,
                "Platform control services for clocks, reset, power, and boot state.",
            )
        )

    if not api_groups:
        # ALWAYS add platform group
        api_groups.append(
            _service_entry(
                "platform",
                [{"name": "platform_probe", "inputs": [], "returns": "PlatformInfo"}],
                "Platform probe and identity service."
            )
        )
        
    public_api_candidates = model.get("public_api_candidates") or []
    if public_api_candidates:
        api_groups.append(
            _service_entry(
                "handoff_candidates",
                [
                    {"name": str(item), "inputs": [], "returns": "candidate"}
                    for item in public_api_candidates[:32]
                ],
                "Candidate APIs derived directly from the upstream handoff.",
            )
        )

    return {
        "package_type": "system_software_api_contract",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": model.get("source_workflow_id"),
        "platform_identity": model.get("platform_identity"),
        "api_style": _api_style_for_lang(target_language, sdk_style),
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
                "visibility": "internal",
                "description": "Internal-only hooks for simulation/runtime validation; not intended for application developers.",
                "methods": [
                    {"name": "attach_runtime_backend", "inputs": ["backend: RuntimeBackend"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
                    {"name": "inject_test_hook", "inputs": ["hook_name: &str"], "returns": "Result<(), SoftwareError>" if target_language == "rust" else "status"},
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
        f"- Public API language: {(contract.get('api_style') or {}).get('public_api_language')}",
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
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if isinstance(state.get("workflow_dir"), str) and state.get("workflow_dir").strip() else ""
    print(f"\n🧩 Running {AGENT_NAME}")

    model = _load_model(state, workflow_dir)
    if not model:
        state["status"] = "❌ capability model missing"
        return state

    contract = _build_api_contract(model, state)
    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "public_group_count": len(contract.get("public_api_groups") or []),
        "target_language": (contract.get("api_style") or {}).get("public_api_language"),
    }

    _record_text(workflow_id, API_JSON, json.dumps(contract, indent=2))
    _record_text(workflow_id, API_MD, _markdown_report(contract))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_api_contract"] = contract
    state["system_software_api_contract_path"] = f"{OUTPUT_SUBDIR}/{API_JSON}"
    state["status"] = "✅ System software API contract generated"
    return state
