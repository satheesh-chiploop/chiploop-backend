import json
from pathlib import Path
from typing import Any, Dict


def _write(root: Path, name: str, value: Dict[str, Any]) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def build_surrogate_architecture_package(
    requirements: Dict[str, Any], model: Dict[str, Any], artifact_dir: str
) -> Dict[str, Any]:
    """Create architecture evidence from a published pretrained-model interface.

    This deliberately does not execute the surrogate or manufacture prediction
    values.  It is the CPU-only architecture-definition mode of the Physical AI
    loop.
    """
    root = Path(artifact_dir) / "surrogate_architecture"
    root.mkdir(parents=True, exist_ok=True)
    parameters = requirements.get("parameters") or {}
    implementation_path = str(requirements.get("implementation_path") or "digital_ip_asic")
    interface = {
        "schema": "chiploop.physical_ai.surrogate_interface.v1",
        "model_id": model["model_id"],
        "model_name": model["name"],
        "provider": model["provider"],
        "checkpoint": model.get("configuration", {}).get("checkpoint") or model.get("reference_checkpoint") or "nvidia/domino_drivaerml",
        "reference_url": model.get("configuration", {}).get("reference_url") or model.get("reference_url") or "https://huggingface.co/nvidia/domino_drivaerml",
        "inputs": model.get("inputs") or ["vehicle_geometry", "flow_conditions"],
        "outputs": model.get("outputs") or ["drag_force", "lift_force", "surface_pressure", "flow_field"],
        "reference_conditions": {
            "stream_velocity_mps": float(parameters.get("stream_velocity_mps") or 38.89),
            "geometry_format": str(parameters.get("geometry_format") or "STL"),
            "geometry_source": str(parameters.get("geometry_source") or "DrivAerML reference geometry"),
        },
        "inference": {
            "status": "not_executed",
            "reason": "Architecture-definition mode does not require a GPU worker or hosted NIM.",
            "future_runtimes": ["NVIDIA-hosted NIM", "ChipLoop GPU worker"],
        },
    }
    architecture = {
        "schema": "chiploop.physical_ai.product_architecture.v1",
        "application": requirements["application"],
        "objective": requirements["objective"],
        "execution_partition": {
            "control_plane": "ChipLoop backend and HEM",
            "source_of_truth": "Supabase runs, model snapshot, status, and artifacts",
            "surrogate_plane": "Optional NVIDIA NIM or GPU worker; not required for this architecture run",
            "silicon_plane": "Digital IP produced by ChipLoop Digital Design and Implementation loops",
        },
        "implementation_path": implementation_path,
        "data_flow": [
            "vehicle geometry and flow conditions",
            "surrogate inference service",
            "pressure/flow/drag engineering results",
            "real-time aero-control policy",
            "digital control IP and actuator commands",
        ],
        "trust_boundaries": ["authenticated job submission", "versioned model identity", "validated input envelope", "no fabricated surrogate results"],
    }
    digital_ip = {
        "schema": "chiploop.digital_ip.spec.v1",
        "name": "adaptive_aero_control_ip",
        "top_module": "adaptive_aero_control_top",
        "project_name": "adaptive_aero_control",
        "purpose": "Low-latency sensor aggregation, safety monitoring, and active-aero actuator control around an external GPU surrogate.",
        "clock": {"name": "clk", "target_frequency_mhz": 100},
        "reset": {"name": "reset_n", "active_low": True},
        "interfaces": ["AXI4-Lite control/status", "streaming pressure-sensor input", "speed and yaw inputs", "actuator command output", "interrupt"],
        "blocks": ["register bank", "sensor filter", "feature aggregator", "command limiter", "safety state machine", "watchdog", "telemetry FIFO"],
        "requirements": [
            "Fail safe when surrogate command is stale or invalid",
            "Clamp every actuator command to software-programmable limits",
            "Expose timestamp, validity, fault, and model-version registers",
            "Permit deterministic fallback control without the GPU service",
        ],
        "downstream_loop": None if implementation_path == "architecture_only" else "Digital Design Loop",
        "surrogate_is_not_rtl": True,
    }
    validation = {
        "schema": "chiploop.physical_ai.validation_plan.v1",
        "architecture_gate": "passed",
        "surrogate_inference_gate": "not_executed",
        "required_before_validated_mode": ["Connect NVIDIA NIM or GPU worker", "Run reference geometry", "Persist real predictions", "Compare against CFD reference evidence"],
        "digital_ip_tests": ["register access", "stale-command timeout", "limit clamping", "fallback transition", "fault interrupt", "telemetry ordering"],
    }
    product_map = {
        "schema": "chiploop.physical_ai.product_map.v1",
        "product": "AI-assisted active aerodynamics controller",
        "implementation_path": implementation_path,
        "milestones": [
            {"id": "m1", "name": "Architecture and digital-IP contract", "status": "completed"},
            {"id": "m2", "name": "RTL and verification", "status": "not_requested" if implementation_path == "architecture_only" else "ready"},
            {"id": "m3", "name": "Digital IP / ASIC implementation", "status": "planned" if implementation_path in {"digital_ip_asic", "fpga_then_asic"} else "not_requested"},
            {"id": "m3_fpga", "name": "FPGA prototype", "status": "planned" if implementation_path in {"fpga_prototype", "fpga_then_asic"} else "not_requested"},
            {"id": "m4", "name": "Surrogate-backed validation", "status": "blocked_on_inference_runtime"},
            {"id": "m5", "name": "Optional FPGA prototype", "status": "optional"},
        ],
    }
    files = {
        "surrogate_interface_contract": _write(root, "surrogate_interface_contract.json", interface),
        "product_architecture": _write(root, "product_architecture.json", architecture),
        "digital_ip_spec": _write(root, "digital_ip_spec.json", digital_ip),
        "validation_plan": _write(root, "validation_plan.json", validation),
        "product_map": _write(root, "product_map.json", product_map),
    }
    return {
        "status": "architecture_ready",
        "execution_mode": "architecture",
        "inference_status": "not_executed",
        "interface": interface,
        "architecture": architecture,
        "digital_ip_spec": digital_ip,
        "validation": validation,
        "product_map": product_map,
        "implementation_path": implementation_path,
        "files": files,
    }
