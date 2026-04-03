import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Capability Model Agent"
OUTPUT_SUBDIR = "system/software/model"
MODEL_JSON = "system_software_capability_model.json"
SUMMARY_MD = "system_software_capability_model.md"


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


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _join_workflow_path(workflow_dir: str, rel_or_abs: str) -> str:
    if not rel_or_abs:
        return ""
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.abspath(os.path.join(workflow_dir, rel_or_abs))


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _load_input_contract(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    contract = state.get("system_software_input_contract")
    if isinstance(contract, dict):
        return contract

    for candidate in (
        state.get("system_software_input_contract_path"),
        "system/software/input/system_software_input_contract.json",
    ):
        if _is_nonempty_str(candidate):
            obj = _safe_read_json(_join_workflow_path(workflow_dir, candidate))
            if obj:
                return obj
    return {}


def _extract_register_summary(register_map_path: str, workflow_dir: str) -> Dict[str, Any]:
    obj = _safe_read_json(_join_workflow_path(workflow_dir, register_map_path))
    regmap = obj.get("regmap") if isinstance(obj.get("regmap"), dict) else obj
    registers = regmap.get("registers") if isinstance(regmap, dict) else None

    summary = {
        "register_count": 0,
        "register_names": [],
        "bus": "",
        "addr_width": None,
        "data_width": None,
    }

    if isinstance(regmap, dict):
        summary["bus"] = regmap.get("bus") or ""
        summary["addr_width"] = regmap.get("addr_width")
        summary["data_width"] = regmap.get("data_width")

    if isinstance(registers, list):
        names = [str(r.get("name")) for r in registers if isinstance(r, dict) and r.get("name")]
        summary["register_names"] = names[:64]
        summary["register_count"] = len(names)

    return summary


def _extract_integration_summary(path: str, workflow_dir: str) -> Dict[str, Any]:
    obj = _safe_read_json(_join_workflow_path(workflow_dir, path))
    connections = obj.get("connections") if isinstance(obj.get("connections"), list) else []
    instances = obj.get("instances") if isinstance(obj.get("instances"), list) else []
    top = obj.get("top") if isinstance(obj.get("top"), dict) else {}
    return {
        "instance_count": len(instances),
        "instance_names": [inst.get("name") for inst in instances if isinstance(inst, dict) and inst.get("name")][:32],
        "connection_count": len(connections),
        "top": top,
    }


def _extract_services(contract: Dict[str, Any]) -> Dict[str, Any]:
    firmware_contract = contract.get("firmware_contract") or {}
    verification_contract = contract.get("verification_contract") or {}

    services = {
        "register_access": bool(firmware_contract.get("register_map_path") and firmware_contract.get("hal_path")),
        "driver_layer": bool(firmware_contract.get("driver_path")),
        "interrupt_control": bool(firmware_contract.get("interrupt_mapping_path")),
        "dma_control": bool(firmware_contract.get("dma_integration_path")),
        "boot_orchestration": bool(firmware_contract.get("boot_dependency_plan_path")),
        "clock_control": bool(firmware_contract.get("clock_config_path")),
        "reset_control": bool(firmware_contract.get("reset_sequence_path")),
        "power_mode_control": bool(firmware_contract.get("power_mode_path")),
        "runtime_validation_support": bool(verification_contract.get("cocotb_makefile_path")),
    }
    return services


def _build_capability_model(contract: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    system_contract = contract.get("system_contract") or {}
    firmware_contract = contract.get("firmware_contract") or {}
    verification_contract = contract.get("verification_contract") or {}
    status = contract.get("input_status") or {}

    register_summary = _extract_register_summary(firmware_contract.get("register_map_path") or "", workflow_dir)
    integration_summary = _extract_integration_summary(system_contract.get("system_integration_intent_path") or "", workflow_dir)
    services = _extract_services(contract)

    return {
        "package_type": "system_software_capability_model",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": contract.get("source_workflow_id"),
        "platform_identity": {
            "top_module": system_contract.get("top_module"),
            "soc_top_sim_path": system_contract.get("soc_top_sim_path"),
            "rtl_file_count": len(system_contract.get("rtl_file_entries") or []),
        },
        "hardware_model": {
            "integration_summary": integration_summary,
            "register_summary": register_summary,
        },
        "software_services": services,
        "runtime_contract": {
            "firmware_elf_exists": firmware_contract.get("elf_exists"),
            "firmware_elf_placeholder": firmware_contract.get("elf_placeholder"),
            "execution_readiness": verification_contract.get("execution_readiness"),
            "execution_status": verification_contract.get("execution_overall_status"),
            "coverage_available": verification_contract.get("coverage_available"),
        },
        "software_constraints": {
            "blocking_gaps": status.get("blocking_gaps") or [],
            "assumptions": status.get("assumptions") or [],
            "input_validation_errors": status.get("errors") or [],
        },
        "public_api_candidates": contract.get("public_api_candidates") or [],
    }


def _markdown_report(model: Dict[str, Any]) -> str:
    lines = [
        "# System Software Capability Model",
        "",
        f"- Generated at: {model.get('generated_at')}",
        f"- Source workflow id: {model.get('source_workflow_id') or 'unavailable'}",
        "",
        "## Platform identity",
        "",
        f"- Top module: `{(model.get('platform_identity') or {}).get('top_module') or 'unavailable'}`",
        f"- RTL file count: `{(model.get('platform_identity') or {}).get('rtl_file_count')}`",
        "",
        "## Software services",
        "",
    ]

    for key, value in (model.get("software_services") or {}).items():
        lines.append(f"- `{key}` → `{value}`")

    lines.extend(["", "## Register model", ""])
    reg = (model.get("hardware_model") or {}).get("register_summary") or {}
    lines.append(f"- Register count: `{reg.get('register_count')}`")
    lines.append(f"- Bus: `{reg.get('bus') or 'unavailable'}`")

    lines.extend(["", "## Constraints", ""])
    cons = model.get("software_constraints") or {}
    for item in (cons.get("blocking_gaps") or []) + (cons.get("assumptions") or []) + (cons.get("input_validation_errors") or []):
        lines.append(f"- {item}")
    if not ((cons.get("blocking_gaps") or []) or (cons.get("assumptions") or []) or (cons.get("input_validation_errors") or [])):
        lines.append("- none")

    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir")
    print(f"\n🧭 Running {AGENT_NAME}")

    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for capability model"
        return state

    workflow_dir = os.path.abspath(workflow_dir)
    contract = _load_input_contract(state, workflow_dir)
    if not isinstance(contract, dict) or not contract:
        state["status"] = "❌ system software input contract missing"
        return state

    model = _build_capability_model(contract, workflow_dir)
    _record_text(workflow_id, MODEL_JSON, json.dumps(model, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_report(model))

    state["system_software_capability_model"] = model
    state["system_software_capability_model_path"] = f"{OUTPUT_SUBDIR}/{MODEL_JSON}"
    state["status"] = "✅ System software capability model generated"
    return state
