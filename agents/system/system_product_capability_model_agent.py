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


def _uart_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "UART Packet Engine Dashboard",
        "device_model": "uart_packet_engine",
        "lineage": lineage,
        "capabilities": [
            {"id": "set_baud_div", "label": "Set baud divider", "kind": "integer", "register": "BAUD_DIV"},
            {"id": "set_packet_len", "label": "Set packet length", "kind": "integer", "register": "PACKET_LEN"},
            {"id": "enable_loopback", "label": "Enable loopback", "kind": "boolean", "register": "CONTROL.LOOPBACK"},
            {"id": "send_packet", "label": "Send packet", "kind": "command", "register": "TX_DATA"},
            {"id": "read_fifo_levels", "label": "Read FIFO levels", "kind": "telemetry", "signals": ["rx_fifo_level", "tx_fifo_level"]},
            {"id": "read_irq_status", "label": "Read interrupt status", "kind": "telemetry", "register": "IRQ_STATUS"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x00", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "LOOPBACK", "bit": 3}, {"name": "IRQ_ENABLE", "bit": 4}]},
            {"name": "BAUD_DIV", "offset": "0x04", "fields": [{"name": "baud_div", "bits": "15:0"}]},
            {"name": "PACKET_LEN", "offset": "0x08", "fields": [{"name": "packet_len", "bits": "7:0"}]},
            {"name": "TX_DATA", "offset": "0x0C", "fields": [{"name": "tx_data", "bits": "7:0"}]},
            {"name": "RX_DATA", "offset": "0x10", "fields": [{"name": "rx_data", "bits": "7:0"}]},
            {"name": "STATUS", "offset": "0x14", "fields": [{"name": "tx_full"}, {"name": "rx_empty"}, {"name": "packet_active"}, {"name": "error"}]},
            {"name": "IRQ_STATUS", "offset": "0x18", "fields": [{"name": "rx_done"}, {"name": "tx_done"}, {"name": "overflow"}, {"name": "framing_error"}, {"name": "packet_done"}]},
        ],
    }


def _image_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "Image DMA Pipeline Dashboard",
        "device_model": "image_dma_pipeline",
        "lineage": lineage,
        "capabilities": [
            {"id": "configure_frame", "label": "Configure frame geometry", "kind": "configuration", "registers": ["WIDTH", "HEIGHT", "STRIDE"]},
            {"id": "select_filter", "label": "Select filter mode", "kind": "enum", "register": "FILTER_MODE"},
            {"id": "set_threshold", "label": "Set threshold", "kind": "integer", "register": "THRESHOLD"},
            {"id": "start_frame", "label": "Start frame processing", "kind": "command", "register": "CONTROL.START"},
            {"id": "read_dma_progress", "label": "Read DMA progress", "kind": "telemetry", "register": "PIXEL_COUNT"},
            {"id": "read_histogram", "label": "Read histogram", "kind": "telemetry", "register": "HISTOGRAM"},
            {"id": "read_irq_status", "label": "Read interrupt status", "kind": "telemetry", "register": "IRQ_STATUS"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x000", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "START", "bit": 1}, {"name": "IRQ_ENABLE", "bit": 3}]},
            {"name": "STATUS", "offset": "0x004", "fields": [{"name": "busy"}, {"name": "frame_done"}, {"name": "dma_rd_busy"}, {"name": "dma_wr_busy"}, {"name": "histogram_done"}]},
            {"name": "SRC_BASE", "offset": "0x008", "fields": [{"name": "source_address", "bits": "31:0"}]},
            {"name": "DST_BASE", "offset": "0x00C", "fields": [{"name": "destination_address", "bits": "31:0"}]},
            {"name": "WIDTH", "offset": "0x010", "fields": [{"name": "width", "bits": "15:0"}]},
            {"name": "HEIGHT", "offset": "0x014", "fields": [{"name": "height", "bits": "15:0"}]},
            {"name": "FILTER_MODE", "offset": "0x01C", "fields": [{"name": "mode", "bits": "2:0"}]},
            {"name": "THRESHOLD", "offset": "0x028", "fields": [{"name": "threshold", "bits": "7:0"}]},
            {"name": "PIXEL_COUNT", "offset": "0x034", "fields": [{"name": "processed_pixels", "bits": "31:0"}]},
            {"name": "HISTOGRAM", "offset": "0x200", "fields": [{"name": "bins", "count": 256, "bits": "15:0"}]},
        ],
        "expected_scale": {
            "target_flipflops": "25000",
            "reason": "Register-based 3x256x8 line buffers, histogram counters, DMA state, pipeline valid/metadata, and control/status registers.",
        },
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    contract = state.get("system_product_collateral_contract") if isinstance(state.get("system_product_collateral_contract"), dict) else {}
    lineage = contract.get("lineage") if isinstance(contract.get("lineage"), dict) else {}
    intent = str(lineage.get("product_intent") or state.get("product_intent") or "").lower()
    if "image" in intent or "dma" in intent or "histogram" in intent or "filter" in intent:
        model = _image_model(lineage)
    elif "uart" in intent or "packet" in intent or "fifo" in intent:
        model = _uart_model(lineage)
    else:
        model = _pwm_model(lineage)
    if model.get("device_model") == "pwm_controller" and "pwm" not in intent and "fan" not in intent:
        model["product_name"] = "Generated Device Control Dashboard"
        model["notes"] = ["Generic mode uses the validated software API shape and a simulator-backed device adapter."]
    _record(workflow_id, "system_product_capability_model.json", model)
    state["system_product_capability_model"] = model
    state["system_product_capability_model_path"] = f"{OUTPUT_SUBDIR}/system_product_capability_model.json"
    state["status"] = "System product capability model generated"
    return state
