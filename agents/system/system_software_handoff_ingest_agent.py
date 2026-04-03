import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Handoff Ingest Agent"
OUTPUT_SUBDIR = "system/software/input"
CONTRACT_JSON = "system_software_input_contract.json"
SUMMARY_MD = "system_software_input_summary.md"
DEBUG_JSON = "system_software_input_debug.json"


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


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


def _find_handoff_manifest_path(state: Dict[str, Any], workflow_dir: str) -> str:
    candidates: List[str] = []

    for key in (
        "system_software_handoff_path",
        "software_handoff_path",
        "system_firmware_handoff_path",
    ):
        value = state.get(key)
        if _is_nonempty_str(value):
            candidates.append(_norm_path(value))

    system_block = state.get("system")
    if isinstance(system_block, dict):
        for key in ("software_handoff_path",):
            value = system_block.get(key)
            if _is_nonempty_str(value):
                candidates.append(_norm_path(value))

    candidates.extend(
        [
            "system/software_handoff/system_software_handoff.json",
            "system_software_handoff.json",
        ]
    )

    for candidate in candidates:
        abs_path = _join_workflow_path(workflow_dir, candidate)
        if os.path.isfile(abs_path):
            return candidate
    return ""


def _validate_handoff_manifest(obj: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    if not obj:
        errors.append("handoff_manifest_missing_or_invalid_json")
        return errors

    if obj.get("package_type") != "system_software_handoff":
        errors.append("unexpected_package_type")

    if not _is_nonempty_str(obj.get("package_version")):
        errors.append("package_version_missing")

    system_contract = obj.get("system_contract")
    if not isinstance(system_contract, dict):
        errors.append("system_contract_missing")
    else:
        if not _is_nonempty_str(system_contract.get("top_module")):
            errors.append("top_module_missing")
        if not _is_nonempty_str(system_contract.get("system_integration_intent_path")):
            errors.append("system_integration_intent_path_missing")

    firmware_contract = obj.get("firmware_contract")
    if not isinstance(firmware_contract, dict):
        errors.append("firmware_contract_missing")
    else:
        for key in ("firmware_manifest_path", "register_map_path", "hal_path", "driver_path"):
            if not _is_nonempty_str(firmware_contract.get(key)):
                errors.append(f"{key}_missing")

    software_inputs = obj.get("software_inputs")
    if not isinstance(software_inputs, dict):
        errors.append("software_inputs_missing")

    return errors


def _build_input_contract(handoff_manifest: Dict[str, Any], handoff_path: str, workflow_dir: str, errors: List[str]) -> Dict[str, Any]:
    system_contract = handoff_manifest.get("system_contract") or {}
    firmware_contract = handoff_manifest.get("firmware_contract") or {}
    verification_contract = handoff_manifest.get("verification_contract") or {}
    software_inputs = handoff_manifest.get("software_inputs") or {}
    readiness = handoff_manifest.get("software_readiness") or {}

    required_artifacts = {}
    for key in software_inputs.get("required_inputs", []) or []:
        source_value = None
        if key in firmware_contract:
            source_value = firmware_contract.get(key)
        elif key in system_contract:
            source_value = system_contract.get(key)
        required_artifacts[key] = {
            "path": _norm_path(source_value),
            "exists": bool(_is_nonempty_str(source_value) and os.path.exists(_join_workflow_path(workflow_dir, source_value))),
        }

    recommended_artifacts = {}
    for key in software_inputs.get("recommended_inputs", []) or []:
        source_value = None
        if key in firmware_contract:
            source_value = firmware_contract.get(key)
        elif key in system_contract:
            source_value = system_contract.get(key)
        elif key in verification_contract:
            source_value = verification_contract.get(key)
        recommended_artifacts[key] = {
            "path": _norm_path(source_value) if not isinstance(source_value, list) else "",
            "paths": source_value if isinstance(source_value, list) else [],
        }

    return {
        "package_type": "system_software_input_contract",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_handoff_manifest_path": handoff_path,
        "source_workflow_id": handoff_manifest.get("source_workflow_id"),
        "source_workflow_type": handoff_manifest.get("source_workflow_type"),
        "input_status": {
            "valid": len(errors) == 0,
            "errors": errors,
            "package_quality": readiness.get("package_quality"),
            "blocking_gaps": readiness.get("blocking_gaps") or [],
            "assumptions": readiness.get("assumptions") or [],
        },
        "system_contract": system_contract,
        "firmware_contract": firmware_contract,
        "verification_contract": verification_contract,
        "required_artifacts": required_artifacts,
        "recommended_artifacts": recommended_artifacts,
        "public_api_candidates": software_inputs.get("public_api_candidates") or [],
    }


def _markdown_report(contract: Dict[str, Any]) -> str:
    status = contract.get("input_status") or {}
    lines = [
        "# System Software Input Contract Summary",
        "",
        f"- Generated at: {contract.get('generated_at')}",
        f"- Source handoff: `{contract.get('source_handoff_manifest_path') or 'missing'}`",
        f"- Source workflow id: `{contract.get('source_workflow_id') or 'unavailable'}`",
        f"- Valid: `{status.get('valid')}`",
        "",
        "## Required artifacts",
        "",
    ]

    required = contract.get("required_artifacts") or {}
    if required:
        for key, item in required.items():
            lines.append(f"- `{key}` → `{item.get('path') or 'missing'}` | exists=`{item.get('exists')}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Validation", ""])
    errors = status.get("errors") or []
    if errors:
        for err in errors:
            lines.append(f"- {err}")
    else:
        lines.append("- no validation errors")

    lines.extend(["", "## Assumptions / gaps", ""])
    for item in (status.get("assumptions") or []) + (status.get("blocking_gaps") or []):
        lines.append(f"- {item}")
    if not ((status.get("assumptions") or []) or (status.get("blocking_gaps") or [])):
        lines.append("- none")

    lines.extend([
        "",
        "## Conclusion",
        "",
        "This artifact is the normalized, validated input contract for System_Software. Downstream software agents should consume this contract rather than scraping firmware artifacts directly.",
        "",
    ])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir")
    print(f"\n📥 Running {AGENT_NAME}")

    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for system software handoff ingest"
        return state

    workflow_dir = os.path.abspath(workflow_dir)
    handoff_path = _find_handoff_manifest_path(state, workflow_dir)
    handoff_obj = _safe_read_json(_join_workflow_path(workflow_dir, handoff_path)) if handoff_path else {}
    errors = _validate_handoff_manifest(handoff_obj)
    contract = _build_input_contract(handoff_obj, handoff_path, workflow_dir, errors)

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workflow_dir": workflow_dir,
        "handoff_manifest_path": handoff_path,
        "validation_errors": errors,
    }

    _record_text(workflow_id, CONTRACT_JSON, json.dumps(contract, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_report(contract))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_input_contract"] = contract
    state["system_software_input_contract_path"] = f"{OUTPUT_SUBDIR}/{CONTRACT_JSON}"
    state["system_software_input_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"

    system_block = state.setdefault("system", {})
    system_block["software_input_contract"] = contract
    system_block["software_input_contract_path"] = state["system_software_input_contract_path"]

    state["status"] = (
        "✅ System software handoff ingested"
        if contract.get("input_status", {}).get("valid")
        else "⚠️ System software handoff ingested with validation issues"
    )
    return state
