from pathlib import Path
from typing import Any, Dict

from physical_ai.motor_control import build_motor_control_package
from physical_ai.surrogate_architecture import build_surrogate_architecture_package
from physical_ai.active_aero_reference import build_active_aero_reference_package


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    model = state["selected_physics_model"]
    requirements = state["requirements_contract"]
    if requirements.get("execution_mode") == "cpu_reference":
        package = build_active_aero_reference_package(requirements, model, str(Path(state["artifact_dir"])))
        return {**state, "physics_execution": package}
    if requirements.get("execution_mode") == "architecture":
        package = build_surrogate_architecture_package(requirements, model, str(Path(state["artifact_dir"])))
        return {**state, "physics_execution": package}
    if model.get("executor") != "pmsm_equation_v1":
        raise ValueError(f"No executor installed for {model['model_id']}")
    payload = {
        **requirements.get("parameters", {}),
        "simulation_mode": "equation",
        "maximum_surrogate_error_percent": requirements["accuracy"]["maximum_error_percent"],
        "model_policy": state.get("model_policy") or {},
    }
    package = build_motor_control_package(payload, str(Path(state["artifact_dir"])))
    return {**state, "physics_execution": package}
