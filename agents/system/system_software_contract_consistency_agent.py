import datetime
import json
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Contract Consistency Agent"
OUTPUT_SUBDIR = "system/software_validation/contract"

REPORT_JSON = "contract_consistency_report.json"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id, filename, content):
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


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    api_contract = state.get("system_software_api_contract") or {}
    sdk_manifest = state.get("system_software_sdk_manifest") or {}

    api_funcs = set(api_contract.get("functions", []))
    sdk_funcs = set(sdk_manifest.get("exposed_functions", []))

    errors = []
    if api_funcs != sdk_funcs:
        errors.append("API and SDK mismatch")

    status = "pass" if not errors else "fail"

    report = {
        "generated_at": _now(),
        "status": status,
        "errors": errors,
    }

    _record(workflow_id, REPORT_JSON, json.dumps(report, indent=2))

    state["contract_status"] = status
    state["status"] = "✅ contract ok" if status == "pass" else "⚠️ contract issues"
    return state
