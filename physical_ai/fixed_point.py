import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple


QUANTIZED_SIGNALS = ("speed_rpm", "id_a", "iq_a", "torque_nm", "vd_v", "vq_v", "winding_temperature_c")


def _choose_signed_format(max_abs: float, word_bits: int = 16) -> Dict[str, Any]:
    magnitude_bits = max(0, int(math.ceil(math.log2(max_abs + 1e-12)))) if max_abs > 1.0 else 0
    fraction_bits = word_bits - 1 - magnitude_bits
    if fraction_bits < 0:
        raise ValueError(f"Signal range {max_abs:g} cannot fit signed {word_bits}-bit fixed point")
    scale = 1 << fraction_bits
    return {
        "word_bits": word_bits,
        "signed": True,
        "magnitude_bits": magnitude_bits,
        "fraction_bits": fraction_bits,
        "q_format": f"Q{magnitude_bits}.{fraction_bits}",
        "scale": scale,
        "minimum": -(1 << (word_bits - 1)) / scale,
        "maximum": ((1 << (word_bits - 1)) - 1) / scale,
        "resolution": 1.0 / scale,
    }


def _quantize(value: float, fmt: Dict[str, Any]) -> Tuple[int, float, bool]:
    scale = int(fmt["scale"])
    raw = int(round(value * scale))
    low, high = -(1 << (int(fmt["word_bits"]) - 1)), (1 << (int(fmt["word_bits"]) - 1)) - 1
    overflow = raw < low or raw > high
    quantized = min(high, max(low, raw))
    return quantized, quantized / scale, overflow


def analyze_fixed_point(
    timeseries: List[Dict[str, float]],
    payload: Dict[str, Any],
    output_dir: Path,
) -> Dict[str, Any]:
    if not timeseries:
        raise ValueError("Fixed-point analysis requires reference vectors")
    word_bits = int(payload.get("fixed_point_word_bits") or 16)
    if word_bits not in {16, 24, 32}:
        raise ValueError("fixed_point_word_bits must be 16, 24, or 32")
    formats = {
        signal: _choose_signed_format(max(abs(float(row[signal])) for row in timeseries), word_bits)
        for signal in QUANTIZED_SIGNALS
    }
    error_limits = min(0.25, float(payload.get("maximum_surrogate_error_percent") or 3.0) / 10.0)
    quantized_rows: List[Dict[str, Any]] = []
    signal_metrics: Dict[str, Dict[str, Any]] = {}
    total_overflows = 0

    for signal in QUANTIZED_SIGNALS:
        maximum_reference = max(abs(float(row[signal])) for row in timeseries) or 1.0
        absolute_errors: List[float] = []
        squared_errors: List[float] = []
        overflows = 0
        for row in timeseries:
            raw, restored, overflow = _quantize(float(row[signal]), formats[signal])
            error = abs(float(row[signal]) - restored)
            absolute_errors.append(error)
            squared_errors.append(error * error)
            overflows += int(overflow)
        total_overflows += overflows
        signal_metrics[signal] = {
            "maximum_absolute_error": max(absolute_errors),
            "rms_error": math.sqrt(sum(squared_errors) / len(squared_errors)),
            "maximum_range_normalized_error_percent": max(absolute_errors) / maximum_reference * 100.0,
            "overflow_count": overflows,
            "passed": overflows == 0 and max(absolute_errors) / maximum_reference * 100.0 <= error_limits,
        }

    for row in timeseries:
        output: Dict[str, Any] = {"time_s": row["time_s"]}
        for signal in QUANTIZED_SIGNALS:
            raw, restored, overflow = _quantize(float(row[signal]), formats[signal])
            output[f"{signal}_ref"] = row[signal]
            output[f"{signal}_raw"] = raw
            output[f"{signal}_fixed"] = restored
            output[f"{signal}_overflow"] = int(overflow)
        quantized_rows.append(output)

    passed = total_overflows == 0 and all(metric["passed"] for metric in signal_metrics.values())
    analysis = {
        "schema": "chiploop.physical_ai.fixed_point_analysis.v1",
        "status": "passed" if passed else "failed",
        "word_bits": word_bits,
        "acceptance": {"maximum_range_normalized_error_percent": error_limits, "overflow_count": 0},
        "formats": formats,
        "signal_metrics": signal_metrics,
        "total_overflow_count": total_overflows,
        "sample_count": len(timeseries),
        "passed": passed,
        "limitations": [
            "This gate quantizes reference I/O vectors; arithmetic-stage bit growth must be verified with generated RTL.",
            "The equation plant remains floating point and is used as the golden comparison model.",
        ],
    }
    current_fmt = formats["iq_a"]
    speed_fmt = formats["speed_rpm"]
    dc_bus_fmt = _choose_signed_format(float(payload.get("dc_bus_voltage_v") or 48.0), word_bits)
    rtl_contract = {
        "schema": "chiploop.physical_ai.rtl_numeric_contract.v1",
        "top_module": "motor_control_top",
        "clock_frequency_hz": int(float(payload.get("target_frequency_mhz") or 50.0) * 1_000_000),
        "control_loop_hz": int(float(payload.get("control_loop_hz") or 20_000.0)),
        "latency_budget_cycles": int(payload.get("latency_budget_cycles") or 1000),
        "rounding": "round_to_nearest",
        "overflow": "saturate_and_raise_fault",
        "ports": {
            "phase_current_a": current_fmt,
            "phase_current_b": current_fmt,
            "rotor_position_turns": {"word_bits": 16, "signed": False, "q_format": "UQ0.16", "fraction_bits": 16},
            "speed_reference_rpm": speed_fmt,
            "speed_measured_rpm": speed_fmt,
            "dc_bus_voltage_v": dc_bus_fmt,
            "duty_u": {"word_bits": 16, "signed": False, "q_format": "UQ0.16", "fraction_bits": 16},
            "duty_v": {"word_bits": 16, "signed": False, "q_format": "UQ0.16", "fraction_bits": 16},
            "duty_w": {"word_bits": 16, "signed": False, "q_format": "UQ0.16", "fraction_bits": 16},
            "pwm_u": {"word_bits": 1, "signed": False, "q_format": "bit", "fraction_bits": 0},
            "pwm_v": {"word_bits": 1, "signed": False, "q_format": "bit", "fraction_bits": 0},
            "pwm_w": {"word_bits": 1, "signed": False, "q_format": "bit", "fraction_bits": 0},
        },
        "internal_formats": formats,
        "required_blocks": ["clarke", "park", "speed_pi", "id_pi", "iq_pi", "inverse_park", "svpwm", "fault_monitor"],
        "golden_vectors": "fixed_point_vectors.csv",
        "acceptance": analysis["acceptance"],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_path = output_dir / "fixed_point_analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")
    contract_path = output_dir / "rtl_numeric_contract.json"
    contract_path.write_text(json.dumps(rtl_contract, indent=2, sort_keys=True), encoding="utf-8")
    vectors_path = output_dir / "fixed_point_vectors.csv"
    with vectors_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(quantized_rows[0]))
        writer.writeheader()
        writer.writerows(quantized_rows)
    return {
        "analysis": analysis,
        "rtl_contract": rtl_contract,
        "files": {
            "fixed_point_analysis": str(analysis_path),
            "fixed_point_vectors": str(vectors_path),
            "rtl_numeric_contract": str(contract_path),
        },
    }
