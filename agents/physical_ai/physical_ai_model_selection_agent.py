from typing import Any, Dict

from physical_ai.model_registry import get_physics_model, list_physics_models


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    requirements = state["requirements_contract"]
    requested = str(state.get("physics_model_id") or "").strip()
    supplied_model = state.get("physics_model_record")
    if isinstance(supplied_model, dict) and supplied_model.get("model_id"):
        model = dict(supplied_model)
        if requested and str(model.get("model_id")) != requested:
            raise ValueError("Supabase physics model record does not match the requested model")
    elif requested:
        model = get_physics_model(requested)
    else:
        matches = [item for item in list_physics_models(include_unavailable=False) if item["domain"] == requirements["physics_domain"]]
        if not matches:
            raise ValueError(f"No ready physics model for domain {requirements['physics_domain']}")
        model = matches[0]
    if model["availability"] != "ready":
        raise ValueError(f"Physics model {model['model_id']} is not executable: {model['availability']}")
    target = requirements["implementation_target"]
    if target not in model["implementation_targets"]:
        raise ValueError(f"Physics model {model['model_id']} does not support target {target}")
    return {**state, "selected_physics_model": model}
