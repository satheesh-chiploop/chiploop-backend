from .motor_control import build_motor_control_package
from .model_registry import get_physics_model, list_physics_models


def run_physical_ai_workflow(*args, **kwargs):
    from .workflow import run_physical_ai_workflow as execute

    return execute(*args, **kwargs)

__all__ = ["build_motor_control_package", "get_physics_model", "list_physics_models", "run_physical_ai_workflow"]
