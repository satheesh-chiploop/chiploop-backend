import datetime
import json
from pathlib import Path
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Product Collateral Ingest Agent"
OUTPUT_SUBDIR = "system/product/input"
ARTIFACT_BUCKET = "artifacts"


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


def _storage_paths(artifacts: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(artifacts, dict):
        for value in artifacts.values():
            paths.extend(_storage_paths(value))
    elif isinstance(artifacts, list):
        for value in artifacts:
            paths.extend(_storage_paths(value))
    elif isinstance(artifacts, str):
        paths.append(artifacts.replace("\\", "/"))
    return paths


def _download_json(state: Dict[str, Any], path: str) -> Dict[str, Any]:
    supabase = state.get("supabase_client")
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(path) if supabase else None
        if raw:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    try:
        local = Path(path)
        if local.exists():
            obj = json.loads(local.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _workflow_artifact_json(state: Dict[str, Any], workflow_id: str, filenames: list[str]) -> Dict[str, Any]:
    supabase = state.get("supabase_client")
    if not supabase or not workflow_id:
        return {}
    paths: list[str] = []
    try:
        row = (
            supabase.table("workflows")
            .select("id,artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
            .data
            or {}
        )
        paths.extend(_storage_paths(row.get("artifacts") or {}))
    except Exception:
        pass
    prefix = f"backend/workflows/{workflow_id}"
    for filename in filenames:
        paths.append(f"{prefix}/{filename}")
    wanted = {name.lower() for name in filenames}
    for path in dict.fromkeys(paths):
        if any(path.lower().endswith(name) for name in wanted):
            obj = _download_json(state, path)
            if obj:
                return {"path": path, "data": obj}
    return {}


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
    source_artifacts = {
        "firmware_dashboard": _workflow_artifact_json(
            state,
            str(lineage.get("firmware_workflow_id") or ""),
            ["system_firmware_dashboard.json", "system/firmware/cosim/system_firmware_dashboard.json"],
        ),
        "software_api": _workflow_artifact_json(
            state,
            str(lineage.get("software_workflow_id") or ""),
            ["system_software_api_contract.json", "system/software/api/system_software_api_contract.json"],
        ),
        "software_package": _workflow_artifact_json(
            state,
            str(lineage.get("software_workflow_id") or ""),
            ["system_software_package.json", "system/software/package/system_software_package.json"],
        ),
        "validation_summary": _workflow_artifact_json(
            state,
            str(lineage.get("validation_workflow_id") or ""),
            [
                "system_software_validation_summary_l2.json",
                "system_software_validation_summary.json",
                "system/cosim/l2_validation_summary.json",
            ],
        ),
    }
    contract = {
        "type": "system_product_collateral_contract",
        "version": "1.0",
        "lineage": lineage,
        "workflow_checks": checks,
        "source_artifacts": source_artifacts,
        "hardware_replacement_path": "The generated simulator adapter can later be replaced by a board/silicon transport while preserving the app API.",
    }
    _record(workflow_id, "system_product_collateral_contract.json", contract)
    state["system_product_collateral_contract"] = contract
    state["system_product_collateral_contract_path"] = f"{OUTPUT_SUBDIR}/system_product_collateral_contract.json"
    state["status"] = "System product collateral ingested"
    return state
