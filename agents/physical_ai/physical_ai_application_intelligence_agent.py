import json
from typing import Any, Dict

from model_gateway import complete_text


def _json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("application intelligence model must return one JSON object")
    return value


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    req = state["requirements_contract"]
    fallback = {
        "schema": "chiploop.application_intelligence.contract.v1",
        "name": req["application"],
        "objective": req["objective"],
        "physics_domain": req["physics_domain"],
        "operating_envelope": req.get("operating_envelope") or {},
        "constraints": {
            "accuracy": req.get("accuracy") or {},
            "safety": req.get("safety_constraints") or [],
            "implementation_path": req.get("implementation_path"),
            "implementation_target": req.get("implementation_target"),
        },
        "required_capabilities": [
            "model discovery and qualification",
            "architecture tradeoff analysis",
            "software firmware hardware partitioning",
            "traceable downstream implementation",
        ],
        "acceptance_gates": [
            "model identity and license recorded",
            "inference status explicitly reported",
            "partition interfaces are bounded",
            "RTL must pass compile and lint before HEM continues",
            "implementation must meet target timing and resource constraints",
        ],
        "source_of_truth": "supabase",
    }
    if not bool(state.get("generate_architecture_with_model", False)):
        return {**state, "application_contract": fallback}

    prompt = f"""You are ChipLoop's Application Intelligence Agent.
Convert the supplied application requirements into a technology-neutral product contract.
Do not choose a board, fabricate model accuracy, or assume pretrained inference ran.
Return JSON only with keys: name, objective, physics_domain, operating_envelope,
constraints, required_capabilities, acceptance_gates, workloads, interfaces.
workloads must describe application jobs without prematurely assigning them to CPU, GPU,
firmware, FPGA, or ASIC. acceptance_gates must be measurable.

REQUIREMENTS:
{json.dumps(req, indent=2, default=str)}
"""
    try:
        generated = _json_object(complete_text(
            prompt,
            capability="planner",
            agent_name="Application Intelligence Agent",
            state=state,
        ))
        for key in ("required_capabilities", "acceptance_gates", "workloads", "interfaces"):
            if not isinstance(generated.get(key), list):
                raise ValueError(f"application intelligence response missing list field {key}")
        application = {**fallback, **generated, "source_of_truth": "supabase", "generation_status": "model_generated"}
    except Exception as exc:
        application = {**fallback, "generation_status": "deterministic_fallback", "generation_warning": f"{type(exc).__name__}: {exc}"}
    return {**state, "application_contract": application}
