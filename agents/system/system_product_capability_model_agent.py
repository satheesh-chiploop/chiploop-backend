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


def _base_experience(
    *,
    summary: str,
    controls: Dict[str, str],
    metrics: Dict[str, str],
    scenario_name: str,
    scenario_steps: list[Dict[str, str]],
    timing_model: str = "",
) -> Dict[str, Any]:
    return {
        "summary": summary,
        "control_explanations": controls,
        "metric_explanations": metrics,
        "scenario_name": scenario_name,
        "scenario_steps": scenario_steps,
        "timing_model": timing_model,
        "hardware_replacement_note": "The generated simulator adapter can later be replaced with UART, JTAG, Ethernet, PCIe, or board-specific transport while preserving the application API.",
    }


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
        "product_experience": _base_experience(
            summary="Fan-control dashboard for a memory-mapped PWM controller. Software configures enable, duty cycle, and period while telemetry shows the internal counter and PWM output.",
            controls={
                "Enable PWM": "Turns the PWM output path on or off through the CONTROL.ENABLE field.",
                "Duty cycle": "Sets how much of each PWM period is high. Higher duty drives the fan harder.",
                "Period": "Sets the number of controller clock ticks in one PWM cycle.",
            },
            metrics={
                "Counter": "Internal PWM position within the current period.",
                "PWM Out": "Current output level driven to the fan-control pin.",
                "PWM Frequency": "Simulated controller clock divided by period.",
                "High Threshold": "Counter value below which PWM output is high.",
            },
            scenario_name="Run thermal scenario",
            scenario_steps=[
                {"label": "25 C", "description": "Fan off policy"},
                {"label": "45 C", "description": "Quiet airflow policy"},
                {"label": "60 C", "description": "Nominal cooling policy"},
                {"label": "80 C", "description": "Thermal recovery policy"},
            ],
            timing_model="Simulated 1 MHz controller clock. PWM frequency = 1,000,000 / period.",
        ),
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
        "product_experience": _base_experience(
            summary="UART packet-engine dashboard for exercising software-driven baud setup, payload transmit, loopback receive, FIFO telemetry, and interrupt recovery.",
            controls={
                "Enable engine": "Turns the UART packet engine on through the CONTROL register.",
                "Loopback mode": "Routes transmitted bytes back to the receive path for board-free validation.",
                "Baud divider": "Controls UART bit timing. Lower divider means higher baud rate.",
                "Packet payload": "Hex bytes written by software into the transmit path.",
            },
            metrics={
                "TX Level": "Bytes queued for transmit.",
                "RX Level": "Bytes available for software to read.",
                "IRQ Status": "Interrupt cause observed by firmware/software.",
                "Estimated Baud": "50 MHz divided by baud divider and 16x oversampling.",
            },
            scenario_name="Run packet scenario",
            scenario_steps=[
                {"label": "Boot", "description": "Engine enabled and FIFOs empty"},
                {"label": "Loopback", "description": "Packet transmitted and received"},
                {"label": "Error", "description": "Framing error raised and cleared"},
            ],
            timing_model="Estimated baud = 50 MHz / baud_divider / 16.",
        ),
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
        "product_experience": _base_experience(
            summary="Image-processing dashboard for a DMA-backed pixel pipeline. Software configures frame parameters and filter mode, starts processing, and observes progress, histogram, and frame-done interrupt state.",
            controls={
                "Filter mode": "Selects the pixel-processing kernel for the frame.",
                "Threshold": "Classification threshold used by threshold and edge-oriented modes.",
                "Brightness": "Signed offset applied to generated pixel values.",
                "Start frame": "Starts one DMA-backed frame-processing transaction.",
            },
            metrics={
                "DMA progress": "Percentage of the 64 x 64 frame that has moved through the pipeline.",
                "Pixels": "Processed pixel count for the current frame.",
                "Histogram": "Distribution of output pixel intensity bins.",
                "IRQ": "Frame completion or error signal visible to software.",
            },
            scenario_name="Run vision scenario",
            scenario_steps=[
                {"label": "Load", "description": "Raw frame moved through bypass"},
                {"label": "Edge", "description": "Edge-detect kernel selected"},
                {"label": "Threshold", "description": "Bright pixels classified and IRQ asserted"},
            ],
            timing_model="Demo frame size is 64 x 64 = 4096 pixels. Production frame geometry can be mapped to the generated register interface.",
        ),
    }


def _sensor_hub_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "Smart Sensor Hub Dashboard",
        "device_model": "smart_sensor_hub_mcu",
        "lineage": lineage,
        "capabilities": [
            {"id": "configure_sample_period", "label": "Configure sample period", "kind": "integer", "register": "SAMPLE_PERIOD"},
            {"id": "select_sensor_channels", "label": "Select sensor channels", "kind": "bitmask", "register": "SENSOR_SELECT"},
            {"id": "set_thresholds", "label": "Set alert thresholds", "kind": "configuration", "registers": ["TEMP_THRESHOLD", "HUMIDITY_THRESHOLD", "AIR_THRESHOLD"]},
            {"id": "read_latest_values", "label": "Read latest sensor values", "kind": "telemetry", "registers": ["LATEST_TEMP", "LATEST_HUMIDITY", "LATEST_AIR"]},
            {"id": "read_fifo_level", "label": "Read FIFO depth", "kind": "telemetry", "register": "FIFO_LEVEL"},
            {"id": "clear_alerts", "label": "Clear alerts", "kind": "command", "register": "ALERT_CLEAR"},
            {"id": "control_low_power", "label": "Control low-power sampling", "kind": "boolean", "register": "CONTROL.LOW_POWER_EN"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x000", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "SAMPLE_START", "bit": 1}, {"name": "LOW_POWER_EN", "bit": 2}, {"name": "IRQ_ENABLE", "bit": 3}]},
            {"name": "STATUS", "offset": "0x004", "fields": [{"name": "busy"}, {"name": "sample_done"}, {"name": "fifo_empty"}, {"name": "fifo_full"}, {"name": "alert_pending"}, {"name": "low_power_active"}]},
            {"name": "SAMPLE_PERIOD", "offset": "0x008", "fields": [{"name": "period_ticks", "bits": "31:0"}]},
            {"name": "SENSOR_SELECT", "offset": "0x00C", "fields": [{"name": "temp_en"}, {"name": "humidity_en"}, {"name": "air_en"}]},
            {"name": "TEMP_THRESHOLD", "offset": "0x010", "fields": [{"name": "temp_threshold", "bits": "15:0"}]},
            {"name": "HUMIDITY_THRESHOLD", "offset": "0x014", "fields": [{"name": "humidity_threshold", "bits": "15:0"}]},
            {"name": "AIR_THRESHOLD", "offset": "0x018", "fields": [{"name": "air_threshold", "bits": "15:0"}]},
            {"name": "FIFO_LEVEL", "offset": "0x02C", "fields": [{"name": "fifo_level", "bits": "5:0"}]},
            {"name": "ALERT_STATUS", "offset": "0x030", "fields": [{"name": "temp_alert"}, {"name": "humidity_alert"}, {"name": "air_alert"}, {"name": "fifo_overflow"}, {"name": "sample_timeout"}]},
            {"name": "SAMPLE_COUNT", "offset": "0x038", "fields": [{"name": "sample_count", "bits": "31:0"}]},
        ],
        "product_experience": _base_experience(
            summary="IoT smart sensor hub dashboard for a microcontroller-style subsystem. Software configures sample rate, enabled channels, thresholds, FIFO handling, alert interrupts, and low-power behavior.",
            controls={
                "Sample period": "Controls how often the sensor hub requests a new sample.",
                "Sensor channels": "Selects temperature, humidity, and air-quality telemetry channels.",
                "Thresholds": "Alert limits used by local hardware comparators before software/cloud upload.",
                "Low-power mode": "Reduces activity when the node is idle or sampling is slowed.",
            },
            metrics={
                "Temperature/Humidity/Air": "Latest sampled telemetry values visible to firmware/software.",
                "FIFO depth": "Buffered sample records waiting for software to drain.",
                "Alert state": "Latched threshold or FIFO events that can raise alert_irq.",
                "Sample count": "Completed sensor sample transactions.",
            },
            scenario_name="Run IoT telemetry scenario",
            scenario_steps=[
                {"label": "Sample", "description": "Collect periodic sensor readings"},
                {"label": "Alert", "description": "Cross threshold and assert IRQ"},
                {"label": "Drain", "description": "Read FIFO and clear alert"},
                {"label": "Low power", "description": "Slow sampling for idle mode"},
            ],
            timing_model="Sample period is modeled as controller clock ticks. Product software can later map this to a real RTC or MCU timer.",
        ),
    }


def _secure_boot_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "Secure Boot Key Manager Dashboard",
        "device_model": "secure_boot_key_manager",
        "lineage": lineage,
        "capabilities": [
            {"id": "select_key_slot", "label": "Select key slot", "kind": "integer", "register": "KEY_SLOT"},
            {"id": "set_image_version", "label": "Set image version", "kind": "integer", "register": "IMAGE_VERSION"},
            {"id": "set_min_version", "label": "Set anti-rollback minimum", "kind": "integer", "register": "MIN_VERSION"},
            {"id": "start_boot_check", "label": "Start authentication", "kind": "command", "register": "CONTROL.START_BOOT_CHECK"},
            {"id": "lock_debug", "label": "Lock debug", "kind": "boolean", "register": "CONTROL.LOCK_DEBUG"},
            {"id": "read_boot_status", "label": "Read boot/security status", "kind": "telemetry", "register": "STATUS"},
            {"id": "read_audit_count", "label": "Read audit count", "kind": "telemetry", "register": "AUDIT_COUNT"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x000", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "START_BOOT_CHECK", "bit": 1}, {"name": "LOCK_DEBUG", "bit": 2}, {"name": "IRQ_ENABLE", "bit": 3}]},
            {"name": "STATUS", "offset": "0x004", "fields": [{"name": "busy"}, {"name": "authenticated"}, {"name": "failed"}, {"name": "rollback_fail"}, {"name": "tamper_seen"}, {"name": "debug_locked"}]},
            {"name": "IMAGE_VERSION", "offset": "0x008", "fields": [{"name": "image_version", "bits": "31:0"}]},
            {"name": "MIN_VERSION", "offset": "0x00C", "fields": [{"name": "min_version", "bits": "31:0"}]},
            {"name": "KEY_SLOT", "offset": "0x010", "fields": [{"name": "key_slot", "bits": "3:0"}]},
            {"name": "LIFECYCLE_STATE", "offset": "0x018", "fields": [{"name": "state", "bits": "3:0"}]},
            {"name": "IRQ_STATUS", "offset": "0x01C", "fields": [{"name": "boot_done"}, {"name": "boot_fail"}, {"name": "tamper"}, {"name": "rollback_fail"}]},
            {"name": "AUDIT_COUNT", "offset": "0x024", "fields": [{"name": "audit_count", "bits": "31:0"}]},
        ],
        "product_experience": _base_experience(
            summary="Root-of-trust dashboard for secure boot and key management. Software selects key slot and image version, starts authentication, locks debug, and observes boot, rollback, tamper, IRQ, lifecycle, and audit state.",
            controls={
                "Key slot": "Selects the active authentication key slot for the boot check.",
                "Image version": "Firmware version presented for anti-rollback comparison.",
                "Minimum version": "Anti-rollback floor below which boot must fail.",
                "Debug lock": "Locks debug access according to lifecycle policy.",
            },
            metrics={
                "Boot status": "Authenticated, failed, rollback, and tamper state.",
                "Security IRQ": "Latched boot, tamper, or rollback event visible to firmware/software.",
                "Lifecycle": "Current lifecycle/debug-lock state.",
                "Audit count": "Number of security-relevant events observed.",
            },
            scenario_name="Run secure boot scenario",
            scenario_steps=[
                {"label": "Authenticate", "description": "Valid image passes boot check"},
                {"label": "Rollback", "description": "Old image version is rejected"},
                {"label": "Tamper", "description": "Tamper event blocks boot"},
                {"label": "Lock", "description": "Debug access is locked"},
            ],
            timing_model="Authentication is simulator-backed; replace the adapter with a real hash accelerator, OTP, or lifecycle controller transport later.",
        ),
    }


def _safety_fault_model(lineage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": "Safety Fault Watchdog Dashboard",
        "device_model": "safety_fault_watchdog",
        "lineage": lineage,
        "capabilities": [
            {"id": "configure_watchdog", "label": "Configure watchdog timeout", "kind": "integer", "register": "WATCHDOG_TIMEOUT"},
            {"id": "feed_heartbeat", "label": "Feed heartbeat", "kind": "command", "signal": "heartbeat"},
            {"id": "configure_fault_mask", "label": "Configure fault mask", "kind": "bitmask", "register": "FAULT_MASK"},
            {"id": "inject_fault", "label": "Inject/report fault", "kind": "command", "signal": "fault_in"},
            {"id": "read_escalation", "label": "Read escalation level", "kind": "telemetry", "register": "ESCALATION_LEVEL"},
            {"id": "ack_reset", "label": "Acknowledge reset", "kind": "command", "signal": "external_reset_done"},
        ],
        "registers": [
            {"name": "CONTROL", "offset": "0x000", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "WATCHDOG_ENABLE", "bit": 1}, {"name": "IRQ_ENABLE", "bit": 2}, {"name": "FAULT_CLEAR", "bit": 3}]},
            {"name": "STATUS", "offset": "0x004", "fields": [{"name": "healthy"}, {"name": "watchdog_expired"}, {"name": "fault_pending"}, {"name": "reset_requested"}, {"name": "escalation_active"}]},
            {"name": "WATCHDOG_TIMEOUT", "offset": "0x008", "fields": [{"name": "timeout_ticks", "bits": "31:0"}]},
            {"name": "FAULT_MASK", "offset": "0x010", "fields": [{"name": "enabled_faults", "bits": "7:0"}]},
            {"name": "FAULT_STATUS", "offset": "0x014", "fields": [{"name": "latched_faults", "bits": "7:0"}]},
            {"name": "ESCALATION_LEVEL", "offset": "0x01C", "fields": [{"name": "level", "bits": "1:0"}]},
            {"name": "IRQ_STATUS", "offset": "0x020", "fields": [{"name": "watchdog"}, {"name": "fault"}, {"name": "reset_request"}, {"name": "escalation"}]},
            {"name": "RESET_COUNT", "offset": "0x028", "fields": [{"name": "reset_count", "bits": "31:0"}]},
        ],
        "product_experience": _base_experience(
            summary="Automotive safety dashboard for watchdog and fault-management behavior. Software configures timeout and fault masks, feeds heartbeat, observes latched faults, escalation, reset request, safety IRQ, and recovery status.",
            controls={
                "Watchdog timeout": "Controller tick budget before missing heartbeat becomes a safety event.",
                "Heartbeat": "Software service action that proves the monitored software is alive.",
                "Fault mask": "Selects which incoming fault pins are safety-relevant.",
                "Reset acknowledge": "Clears reset request after external reset handling completes.",
            },
            metrics={
                "Fault status": "Latched enabled fault inputs.",
                "Escalation level": "None, warning, reset, or shutdown policy state.",
                "Reset request": "Hardware request to recover from repeated or severe faults.",
                "Safety IRQ": "Interrupt visible to diagnostic firmware/software.",
            },
            scenario_name="Run safety scenario",
            scenario_steps=[
                {"label": "Healthy", "description": "Heartbeat keeps watchdog clear"},
                {"label": "Timeout", "description": "Missing heartbeat expires watchdog"},
                {"label": "Fault", "description": "Enabled fault latches and raises IRQ"},
                {"label": "Recover", "description": "Reset request is acknowledged"},
            ],
            timing_model="Watchdog timeout is modeled in controller ticks. Replace the simulator adapter with MCU timer or safety island transport later.",
        ),
    }


def _generic_model(lineage: Dict[str, Any], intent: str) -> Dict[str, Any]:
    clean_intent = " ".join(str(intent or "").split())
    product_name = "Generated Device Control Dashboard"
    if clean_intent:
        product_name = f"{clean_intent[:56].strip().title()} Dashboard"
    capabilities = [
        {"id": "configure_device", "label": "Configure device", "kind": "configuration", "register": "CONTROL"},
        {"id": "start_operation", "label": "Start operation", "kind": "command", "register": "CONTROL.START"},
        {"id": "read_status", "label": "Read status", "kind": "telemetry", "register": "STATUS"},
        {"id": "read_result", "label": "Read result", "kind": "telemetry", "register": "RESULT"},
        {"id": "clear_interrupt", "label": "Clear interrupt", "kind": "command", "register": "IRQ_STATUS"},
    ]
    return {
        "type": "system_product_capability_model",
        "version": "1.0",
        "generated_at": _now(),
        "product_name": product_name,
        "device_model": "generic_device_control",
        "lineage": lineage,
        "capabilities": capabilities,
        "registers": [
            {"name": "CONTROL", "offset": "0x00", "fields": [{"name": "ENABLE", "bit": 0}, {"name": "START", "bit": 1}]},
            {"name": "STATUS", "offset": "0x04", "fields": [{"name": "busy"}, {"name": "done"}, {"name": "error"}]},
            {"name": "CONFIG", "offset": "0x08", "fields": [{"name": "mode", "bits": "3:0"}, {"name": "level", "bits": "15:8"}]},
            {"name": "RESULT", "offset": "0x0C", "fields": [{"name": "result_value", "bits": "31:0"}]},
            {"name": "IRQ_STATUS", "offset": "0x10", "fields": [{"name": "done_irq"}, {"name": "error_irq"}]},
        ],
        "product_experience": _base_experience(
            summary=(
                f"Generated product dashboard for: {clean_intent}."
                if clean_intent
                else "Generated product dashboard based on the available RTL, firmware, software, and validation collateral."
            ),
            controls={
                "Enable": "Enables the generated simulator adapter for this device.",
                "Mode": "Selects an operating mode derived from the product intent and generated API shape.",
                "Level": "Represents a tunable configuration value that software writes through the generated control path.",
                "Start operation": "Kicks off one device transaction and updates live status telemetry.",
            },
            metrics={
                "Status": "Current device execution state.",
                "Progress": "How far the active transaction has advanced.",
                "Result": "Representative output value returned by the generated software-visible API.",
                "IRQ": "Completion or error interrupt state visible to firmware/software.",
            },
            scenario_name="Run product scenario",
            scenario_steps=[
                {"label": "Configure", "description": "Apply mode and level settings"},
                {"label": "Execute", "description": "Start one simulated transaction"},
                {"label": "Observe", "description": "Read status, result, and IRQ"},
            ],
            timing_model="Generic simulator mode advances progress in discrete application ticks. Replace with board or silicon transport when hardware is available.",
        ),
        "notes": ["Generic mode uses the validated software API shape and a simulator-backed device adapter."],
    }


def _source_data(contract: Dict[str, Any], key: str) -> Dict[str, Any]:
    source = contract.get("source_artifacts") if isinstance(contract.get("source_artifacts"), dict) else {}
    entry = source.get(key) if isinstance(source.get(key), dict) else {}
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    return data


def _registers_from_sources(contract: Dict[str, Any]) -> list[Dict[str, Any]]:
    for key in ("software_api", "software_package", "firmware_dashboard", "validation_summary"):
        data = _source_data(contract, key)
        for candidate in (
            data.get("registers"),
            data.get("register_map"),
            (data.get("mmio") or {}).get("registers") if isinstance(data.get("mmio"), dict) else None,
            (data.get("api") or {}).get("registers") if isinstance(data.get("api"), dict) else None,
        ):
            if isinstance(candidate, list) and candidate:
                return [item for item in candidate if isinstance(item, dict)]
            if isinstance(candidate, dict):
                regs = candidate.get("registers")
                if isinstance(regs, list) and regs:
                    return [item for item in regs if isinstance(item, dict)]
    return []


def _model_from_registers(lineage: Dict[str, Any], intent: str, registers: list[Dict[str, Any]]) -> Dict[str, Any]:
    base = _generic_model(lineage, intent)
    normalized: list[Dict[str, Any]] = []
    capabilities: list[Dict[str, Any]] = []
    for idx, reg in enumerate(registers[:24]):
        name = str(reg.get("name") or reg.get("register") or reg.get("id") or f"REG_{idx}").upper()
        offset = reg.get("offset") or reg.get("address") or reg.get("addr")
        fields = reg.get("fields") if isinstance(reg.get("fields"), list) else []
        normalized.append({"name": name, "offset": offset or f"0x{idx * 4:02X}", "fields": fields})
        lname = name.lower()
        if any(token in lname for token in ("control", "cfg", "config", "enable", "start")):
            kind = "configuration"
            label = f"Configure {name}"
        elif any(token in lname for token in ("status", "irq", "done", "fail", "error")):
            kind = "telemetry"
            label = f"Read {name}"
        elif any(token in lname for token in ("data", "result", "fifo", "mem")):
            kind = "telemetry"
            label = f"Observe {name}"
        else:
            kind = "register_access"
            label = f"Access {name}"
        capabilities.append({"id": name.lower(), "label": label, "kind": kind, "register": name})
    base["registers"] = normalized
    base["capabilities"] = capabilities[:12] or base["capabilities"]
    base["device_model"] = "register_mapped_device"
    base["notes"] = ["Derived from upstream firmware/software register collateral."]
    base["product_experience"]["summary"] = (
        "Simulator-backed dashboard generated from upstream firmware/software register collateral."
    )
    return base


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = state.get("workflow_id") or "default"
    contract = state.get("system_product_collateral_contract") if isinstance(state.get("system_product_collateral_contract"), dict) else {}
    lineage = contract.get("lineage") if isinstance(contract.get("lineage"), dict) else {}
    intent = str(lineage.get("product_intent") or state.get("product_intent") or "").lower()
    if "image" in intent or "dma" in intent or "histogram" in intent or "filter" in intent:
        model = _image_model(lineage)
    elif "uart" in intent or "packet" in intent or "fifo" in intent:
        model = _uart_model(lineage)
    elif "sensor" in intent or "iot" in intent or "telemetry" in intent or "humidity" in intent or "air-quality" in intent or "low-power" in intent:
        model = _sensor_hub_model(lineage)
    elif "secure" in intent or "root-of-trust" in intent or "key manager" in intent or "rollback" in intent or "tamper" in intent:
        model = _secure_boot_model(lineage)
    elif "safety" in intent or "watchdog" in intent or "fault" in intent or "automotive" in intent or "escalation" in intent:
        model = _safety_fault_model(lineage)
    elif "pwm" in intent or "fan" in intent:
        model = _pwm_model(lineage)
    else:
        registers = _registers_from_sources(contract)
        model = _model_from_registers(lineage, intent, registers) if registers else _generic_model(lineage, intent)
    _record(workflow_id, "system_product_capability_model.json", model)
    state["system_product_capability_model"] = model
    state["system_product_capability_model_path"] = f"{OUTPUT_SUBDIR}/system_product_capability_model.json"
    state["status"] = "System product capability model generated"
    return state
