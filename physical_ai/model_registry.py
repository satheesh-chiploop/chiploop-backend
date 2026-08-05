from copy import deepcopy
from typing import Any, Dict, List


_MODELS: Dict[str, Dict[str, Any]] = {
    "chiploop.pmsm.dq.v1": {
        "model_id": "chiploop.pmsm.dq.v1",
        "name": "PMSM dq Equation Model",
        "provider": "ChipLoop",
        "domain": "motor_control",
        "runtime": "cpu_equation",
        "availability": "ready",
        "training_required": False,
        "gpu_required": False,
        "implementation_targets": ["software", "fpga", "asic"],
        "executor": "pmsm_equation_v1",
        "inputs": ["dc_bus_voltage_v", "rated_speed_rpm", "load_torque_nm", "control_loop_hz"],
        "outputs": ["speed_rpm", "id_a", "iq_a", "torque_nm", "winding_temperature_c"],
    },
    "nvidia.domino.automotive_aero": {
        "model_id": "nvidia.domino.automotive_aero",
        "name": "NVIDIA DoMINO Automotive Aero",
        "provider": "NVIDIA",
        "domain": "automotive_aerodynamics",
        "runtime": "remote_nim",
        "availability": "requires_gpu_worker",
        "training_required": False,
        "gpu_required": True,
        "implementation_targets": ["gpu_service"],
        "executor": None,
        "architecture_definition_supported": True,
        "reference_checkpoint": "nvidia/domino_drivaerml",
        "reference_url": "https://huggingface.co/nvidia/domino_drivaerml",
        "inputs": ["vehicle_geometry", "flow_conditions"],
        "outputs": ["drag_force", "lift_force", "surface_pressure", "flow_field"],
    },
}


def list_physics_models(*, include_unavailable: bool = True) -> List[Dict[str, Any]]:
    models = list(_MODELS.values())
    if not include_unavailable:
        models = [model for model in models if model["availability"] == "ready"]
    return deepcopy(models)


def get_physics_model(model_id: str) -> Dict[str, Any]:
    model = _MODELS.get(str(model_id))
    if not model:
        raise ValueError(f"Unknown physics model: {model_id}")
    return deepcopy(model)
