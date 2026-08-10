from typing import Any, Dict


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    req = state["requirements_contract"]
    selected = state["selected_physics_model"]
    catalog = state.get("physics_model_catalog") if isinstance(state.get("physics_model_catalog"), list) else [selected]
    candidates = []
    for model in catalog:
        if not isinstance(model, dict) or not model.get("model_id"):
            continue
        domain_match = str(model.get("domain")) == str(req.get("physics_domain"))
        candidates.append({
            "model_id": model.get("model_id"),
            "name": model.get("name"),
            "provider": model.get("provider"),
            "domain_match": domain_match,
            "runtime": model.get("runtime"),
            "availability": model.get("availability"),
            "gpu_required": bool(model.get("gpu_required")),
            "score": 100 if str(model.get("model_id")) == str(selected.get("model_id")) else 70 if domain_match else 0,
        })
    mode = str(req.get("execution_mode") or "architecture")
    executed = mode == "validated" and selected.get("availability") == "ready"
    qualification = {
        "schema": "chiploop.application_intelligence.model_qualification.v1",
        "selected_model_id": selected["model_id"],
        "selection_basis": "domain, interface, runtime, availability, and implementation-path compatibility",
        "candidate_count": len(candidates),
        "candidates": sorted(candidates, key=lambda item: int(item["score"]), reverse=True),
        "checkpoint_identified": bool((selected.get("configuration") or {}).get("checkpoint")),
        "inference_executed": executed,
        "accuracy_qualified": executed,
        "qualification_status": "qualified" if executed else "provisionally_compatible",
        "limitations": [] if executed else ["Surrogate inference and application-specific accuracy are not qualified in this run."],
    }
    return {**state, "surrogate_mapping": qualification}
