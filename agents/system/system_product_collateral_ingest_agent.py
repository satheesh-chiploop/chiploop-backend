import datetime
import json
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Product Collateral Ingest Agent"
OUTPUT_SUBDIR = "system/product/input"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, obj: Dict[str, Any]) -> Optional[str]:
    return save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        OUTPUT_SUBDIR,
        filename,
        json.dumps(obj, indent=2),
    )


def _workflow_status(state: Dict[str, Any], workflow_id: str) -> Dict[str, Any]:
    supabase = state.get("supabase_client")
    if not supabase or not workflow_id:
        return {"workflow_id": workflow_id, "status": "not_checked"}
    try:
        row = (
            supabase.table("workflows")
            .select("id,name,loop_type,status,phase,updated_at")
            .eq("id", workflow_id)
            .single()
            .execute()
            .data
            or {}
        )
        return row or {"workflow_id": workflow_id, "status": "not_found"}
    except Exception as exc:
        return {"workflow_id": workflow_id, "status": "lookup_failed", "error": str(exc)}


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    lineage = {
        "generated_at": _now(),
        "arch2rtl_workflow_id": state.get("arch2rtl_workflow_id") or state.get("system_rtl_workflow_id"),
        "verify_workflow_id": state.get("verify_workflow_id"),
        "firmware_workflow_id": state.get("system_firmware_workflow_id") or state.get("firmware_workflow_id"),
        "software_workflow_id": state.get("system_software_workflow_id") or state.get("software_workflow_id"),
        "validation_workflow_id": state.get("system_validation_workflow_id") or state.get("validation_workflow_id"),
        "product_intent": state.get("product_intent") or state.get("goal") or "",
        "target_runtime": state.get("target_runtime") or "simulated_device",
        "app_type": state.get("app_type") or "web_dashboard",
    }
    checks = {
        key: _workflow_status(state, value)
        for key, value in lineage.items()
        if key.endswith("_workflow_id") and isinstance(value, str) and value
    }
    contract = {
        "type": "system_product_collateral_contract",
        "version": "1.0",
        "lineage": lineage,
        "workflow_checks": checks,
        "hardware_replacement_path": "The generated simulator adapter can later be replaced by a board/silicon transport while preserving the app API.",
    }
    _record(workflow_id, "system_product_collateral_contract.json", contract)
    state["system_product_collateral_contract"] = contract
    state["system_product_collateral_contract_path"] = f"{OUTPUT_SUBDIR}/system_product_collateral_contract.json"
    state["status"] = "System product collateral ingested"
    return state
