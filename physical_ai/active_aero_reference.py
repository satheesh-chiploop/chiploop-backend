import json
from pathlib import Path
from typing import Any, Dict

from .surrogate_architecture import build_surrogate_architecture_package


def _write(root: Path, name: str, value: Any) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def build_active_aero_reference_package(requirements: Dict[str, Any], model: Dict[str, Any], artifact_dir: str) -> Dict[str, Any]:
    """Run a transparent CPU aerodynamic reference; never label it DoMINO inference."""
    package = build_surrogate_architecture_package(requirements, model, artifact_dir)
    root = Path(artifact_dir) / "active_aero_reference"
    params = requirements.get("parameters") or {}
    rho = float(params.get("air_density_kg_m3") or 1.225)
    area = float(params.get("frontal_area_m2") or 2.2)
    base_cd = float(params.get("baseline_drag_coefficient") or 0.30)
    speeds = (requirements.get("operating_envelope") or {}).get("stream_velocity_mps") or [20, 30, 40, 50, 55]
    if len(speeds) == 2:
        low, high = float(speeds[0]), float(speeds[1])
        speeds = [low + (high - low) * index / 4.0 for index in range(5)]
    rows = []
    for speed in [float(value) for value in speeds]:
        actuator_percent = max(0.0, min(100.0, (speed - 20.0) * 2.0))
        effective_cd = base_cd * (1.0 - 0.08 * actuator_percent / 100.0)
        drag_n = 0.5 * rho * speed * speed * effective_cd * area
        rows.append({
            "speed_mps": round(speed, 4),
            "actuator_command_percent": round(actuator_percent, 4),
            "effective_drag_coefficient": round(effective_cd, 6),
            "estimated_drag_force_n": round(drag_n, 4),
        })
    reference = {
        "schema": "chiploop.physical_ai.cpu_reference.v1",
        "model_type": "analytical_drag_reference",
        "equation": "drag_force = 0.5 * air_density * velocity^2 * drag_coefficient * frontal_area",
        "surrogate_model_id": model["model_id"],
        "surrogate_inference_status": "not_executed",
        "purpose": "Exercise application partitioning and controller implementation without claiming pretrained-surrogate predictions.",
        "parameters": {"air_density_kg_m3": rho, "frontal_area_m2": area, "baseline_drag_coefficient": base_cd},
        "operating_points": rows,
    }
    policy = {
        "schema": "chiploop.physical_ai.control_policy.v1",
        "format": "piecewise_linear_lut",
        "input": "vehicle_speed_mps",
        "output": "actuator_command_percent",
        "entries": [{"x": row["speed_mps"], "y": row["actuator_command_percent"]} for row in rows],
        "qualification": "cpu_reference_only",
    }
    package["execution_mode"] = "cpu_reference"
    package["inference_status"] = "not_executed"
    package["status"] = "cpu_reference_ready"
    package["metrics"] = {"operating_point_count": len(rows), "cpu_reference_executed": True, "surrogate_inference_executed": False}
    package["cpu_reference"] = reference
    package["control_policy"] = policy
    package["validation"]["cpu_reference_gate"] = "passed"
    package["validation"]["surrogate_inference_gate"] = "not_executed"
    package["files"]["cpu_reference_results"] = _write(root, "cpu_reference_results.json", reference)
    package["files"]["control_policy"] = _write(root, "control_policy.json", policy)
    return package
