import datetime
import json
from typing import Any, Dict, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Product Capability Model Agent"
OUTPUT_SUBDIR = "system/product/model"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, obj: Dict[str, Any]) -> Optional[str]:
    return save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, filename, json.dumps(obj, indent=2))


def _pwm_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "PWM Fan Control Dashboard",
        "device_model": "pwm_controller",
        "lineage": lineage,
        "capabilities": [
            {"id": "enable_pwm", "label": "Enable PWM", "kind": "boolean", "register": "CONTROL.ENABLE"},
            {"id": "set_duty_cycle", "label": "Set duty cycle", "kind": "percent", "register": "DUTY_CYCLE"},
            {"id": "set_period", "label": "Set period", "kind": "integer", "register": "PERIOD"},
            {"id": "read_counter", "label": "Read counter", "kind": "telemetry", "signal": "counter_value"},
            {"id": "read_pwm_out", "label": "Read PWM output", "kind": "telemetry", "signal": "pwm_out"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x00", "fields": [{"name": "ENABLE", "bit": 0}]},
            {"name": "DUTY_CYCLE", "offset": "0x04", "fields": [{"name": "duty_cycle", "bits": "7:0"}]},
            {"name": "PERIOD", "offset": "0x08", "fields": [{"name": "period", "bits": "7:0"}]},
            {"name": "COUNTER_VALUE", "offset": "0x0C", "fields": [{"name": "counter_value", "bits": "7:0"}]},
        ],
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    contract = state.get("system_product_collateral_contract") if isinstance(state.get("system_product_collateral_contract"), dict) else {}
    lineage = contract.get("lineage") if isinstance(contract.get("lineage"), dict) else {}
    intent = str(lineage.get("product_intent") or state.get("product_intent") or "").lower()
    model = _pwm_model(lineage)
    if "pwm" not in intent and "fan" not in intent:
        model["product_name"] = "Generated Device Control Dashboard"
        model["notes"] = ["Generic mode uses the validated software API shape and a simulator-backed device adapter."]
    _record(workflow_id, "system_product_capability_model.json", model)
    state["system_product_capability_model"] = model
    state["system_product_capability_model_path"] = f"{OUTPUT_SUBDIR}/system_product_capability_model.json"
    state["status"] = "System product capability model generated"
    return state
