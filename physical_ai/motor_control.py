import json
import os
from pathlib import Path
from typing import Any, Dict

from model_gateway.policies import physical_ai_agent_assignments
from .fixed_point import analyze_fixed_point
from .pmsm_equations import run_operating_sweep, simulate_pmsm
from .rtl_motor import generate_motor_rtl


DEFAULT_BOARD = "orangecrab_ecp5_85f"


def _write_json(root: Path, name: str, value: Dict[str, Any]) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def build_motor_control_package(payload: Dict[str, Any], artifact_dir: str) -> Dict[str, Any]:
    root = Path(artifact_dir) / "physical_ai" / "motor_control"
    root.mkdir(parents=True, exist_ok=True)
    policy = payload.get("model_policy") or {"mode": "standard", "selected_model": "chiploop_default"}
    board = str(payload.get("board") or DEFAULT_BOARD)
    simulation_mode = str(payload.get("simulation_mode") or "equation")
    if simulation_mode != "equation":
        raise ValueError("Only simulation_mode=equation is available in this milestone")

    contract = {
        "schema": "chiploop.physical_ai.motor_control.v1",
        "application": "pmsm_motor_control_and_fault_detection",
        "motor": {
            "type": "PMSM",
            "dc_bus_voltage_v": float(payload.get("dc_bus_voltage_v") or 48.0),
            "rated_speed_rpm": float(payload.get("rated_speed_rpm") or 3000.0),
            "control_loop_hz": float(payload.get("control_loop_hz") or 20000.0),
            "pole_pairs": int(payload.get("pole_pairs") or 4),
        },
        "physics_model": {
            "framework": "ChipLoop deterministic equation solver",
            "mode": simulation_mode,
            "strategy": "surface_pmsm_dq_equations",
            "reference_outputs": ["id", "iq", "torque_nm", "speed_rpm", "winding_temperature_c"],
            "equations": ["dq electrical dynamics", "electromagnetic torque", "mechanical rotor dynamics", "lumped winding thermal model"],
            "future_mode": "gpu_surrogate",
            "status": "executable",
        },
        "fpga": {
            "board": board,
            "adc_boundary": "abstract_signed_int16_sample_stream",
            "dac_boundary": "abstract_signed_int16_command_stream",
            "blocks": ["clarke", "park", "speed_pi", "id_iq_current_control", "inverse_park", "fault_monitor", "svpwm"],
            "hard_safety_limits_independent_of_ai": True,
        },
        "acceptance": {
            "rtl_matches_fixed_point_reference": True,
            "timing_must_pass": True,
            "maximum_surrogate_error_percent": float(payload.get("maximum_surrogate_error_percent") or 3.0),
        },
        "model_policy": policy,
    }
    assignments = physical_ai_agent_assignments(policy)
    agent_workflow = {
        "schema": "chiploop.nemo_agent_workflow.v1",
        "runtime": "nvidia_nemo_agent_toolkit",
        "execution_owner": "chiploop",
        "agents": assignments,
        "stages": list(assignments.keys()),
        "note": "NAT coordinates model/tool calls; ChipLoop owns run state, approvals, artifacts, and EDA execution.",
    }
    simulation = simulate_pmsm(payload, root)
    sweep = run_operating_sweep(payload, root)
    fixed_point = analyze_fixed_point(simulation["timeseries"], payload, root)
    rtl = generate_motor_rtl(payload, root, fixed_point["rtl_contract"])
    register_map = {
        "schema": "chiploop.register_map.v1",
        "block_name": "motor_control",
        "base_address": "0x40000000",
        "address_width": 8,
        "data_width": 32,
        "top_module": "motor_control_mmio_top",
        "registers": [
            {"name": "CONTROL", "offset": "0x00", "access": "RW", "fields": [{"name": "enable", "bit_offset": 0, "bit_width": 1, "access": "RW"}, {"name": "clear_fault", "bit_offset": 1, "bit_width": 1, "access": "WO"}]},
            {"name": "SPEED_REFERENCE", "offset": "0x04", "access": "RW", "fields": [{"name": "rpm_q", "bit_offset": 0, "bit_width": 16, "access": "RW"}]},
            {"name": "STATUS", "offset": "0x08", "access": "RO", "fields": [{"name": "fault", "bit_offset": 0, "bit_width": 1, "access": "RO"}, {"name": "command_valid", "bit_offset": 1, "bit_width": 1, "access": "RO"}]},
            {"name": "SPEED_MEASURED", "offset": "0x0C", "access": "RO"},
            {"name": "PHASE_CURRENT_A", "offset": "0x10", "access": "RO"},
            {"name": "PHASE_CURRENT_B", "offset": "0x14", "access": "RO"},
            {"name": "DUTY_U", "offset": "0x18", "access": "RO"},
            {"name": "DUTY_V", "offset": "0x1C", "access": "RO"},
            {"name": "DUTY_W", "offset": "0x20", "access": "RO"},
            {"name": "DC_BUS_VOLTAGE", "offset": "0x24", "access": "RO"},
            {"name": "ROTOR_POSITION", "offset": "0x28", "access": "RO"},
        ],
        "safety": {"reset_state": "disabled", "enable_required": True, "clear_fault": "write_one_pulse"},
    }
    hardware_validation = {
        "schema": "chiploop.physical_ai.hardware_validation.v1",
        "status": "approval_required",
        "automatic_execution": False,
        "prerequisites": [
            "Confirm selected FPGA board and ADC/DAC or external converter pin mapping",
            "Verify gate-driver dead time and hardware over-current shutdown independently of FPGA logic",
            "Run firmware/RTL co-simulation with CONTROL.enable remaining zero after reset",
            "Approve a current-limited bench setup with the motor mechanically unloaded",
        ],
        "approval_actions": ["program_fpga", "enable_gate_driver", "energize_motor"],
        "first_power_limits": {"speed_reference_rpm": 100, "current_limit_percent_of_rated": 10, "maximum_duration_seconds": 10},
        "evidence_required": ["pin_mapping", "scope_capture_pwm_dead_time", "fault_shutdown_test", "operator_approval"],
    }
    physics_job = {
        "schema": "chiploop.gpu_job.v1",
        "job_type": "physicsnemo_motor_surrogate_future",
        "container": os.getenv("PHYSICSNEMO_CONTAINER", "nvcr.io/nvidia/physicsnemo/physicsnemo:latest"),
        "enabled": False,
        "executor_url_env": "PHYSICSNEMO_EXECUTOR_URL",
        "inputs": {"design_contract": "physical_ai_design_contract.json", "equation_baseline": "equation_timeseries.csv"},
        "outputs": ["checkpoint", "validation_metrics.json", "compact_surrogate.onnx"],
        "guardrails": ["operating_envelope_check", "out_of_distribution_check", "reference_equation_comparison"],
    }
    fpga_handoff = {
        "schema": "chiploop.fpga_handoff.v1",
        "board": board,
        "top_module": "motor_control_top",
        "target_frequency_mhz": float(payload.get("target_frequency_mhz") or 50.0),
        "stream_contract": {
            "inputs": ["sample_valid", "phase_current_a", "phase_current_b", "rotor_position_turns", "speed_reference_rpm", "speed_measured_rpm", "dc_bus_voltage_v"],
            "outputs": ["command_valid", "duty_u", "duty_v", "duty_w", "pwm_u", "pwm_v", "pwm_w", "fault"],
            "sample_format": "Per-port formats are defined by rtl_numeric_contract.json",
        },
        "next_app": "/apps/fpga-target-explorer",
    }
    files = {
        "design_contract": _write_json(root, "physical_ai_design_contract.json", contract),
        "agent_workflow": _write_json(root, "nemo_agent_workflow.json", agent_workflow),
        "physicsnemo_job": _write_json(root, "physicsnemo_job.json", physics_job),
        "fpga_handoff": _write_json(root, "fpga_handoff.json", fpga_handoff),
        "digital_regmap": _write_json(root, "digital_regmap.json", register_map),
        "hardware_validation_plan": _write_json(root, "hardware_validation_plan.json", hardware_validation),
        **simulation["files"],
        **sweep["files"],
        **fixed_point["files"],
        **rtl["files"],
    }
    summary = {"application": contract["application"], "status": "rtl_smoke_verified" if rtl["manifest"]["verification"]["smoke_passed"] else "rtl_generated", "files": files, "contract": contract, "simulation": {"metrics": simulation["metrics"]}, "operating_sweep": sweep["result"], "fixed_point": fixed_point["analysis"], "rtl_numeric_contract": fixed_point["rtl_contract"], "rtl": rtl["manifest"], "agent_workflow": agent_workflow, "physicsnemo_job": physics_job, "fpga_handoff": fpga_handoff}
    _write_json(root, "physical_ai_summary.json", summary)
    return summary
