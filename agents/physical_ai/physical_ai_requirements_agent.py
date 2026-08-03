from typing import Any, Dict


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = state.get("requirements") if isinstance(state.get("requirements"), dict) else state
    application = str(raw.get("application") or "pmsm_motor_control")
    requirements = {
        "schema": "chiploop.physical_ai.requirements.v1",
        "application": application,
        "objective": str(raw.get("objective") or "Validate a physical model and create an implementation handoff"),
        "physics_domain": str(raw.get("physics_domain") or "motor_control"),
        "operating_envelope": raw.get("operating_envelope") or {},
        "accuracy": {"maximum_error_percent": float(raw.get("maximum_error_percent") or 3.0)},
        "implementation_target": str(raw.get("implementation_target") or "fpga"),
        "safety_constraints": list(raw.get("safety_constraints") or []),
        "parameters": dict(raw.get("parameters") or {}),
        "approved": True,
    }
    return {**state, "requirements_contract": requirements}
