from pathlib import Path
from typing import Any, Dict

from physical_ai.motor_control import build_motor_control_package


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    model = state["selected_physics_model"]
    if model.get("executor") != "pmsm_equation_v1":
        raise ValueError(f"No executor installed for {model['model_id']}")
    requirements = state["requirements_contract"]
    payload = {
        **requirements.get("parameters", {}),
        "simulation_mode": "equation",
        "maximum_surrogate_error_percent": requirements["accuracy"]["maximum_error_percent"],
        "model_policy": state.get("model_policy") or {},
    }
    package = build_motor_control_package(payload, str(Path(state["artifact_dir"])))
    return {**state, "physics_execution": package}
