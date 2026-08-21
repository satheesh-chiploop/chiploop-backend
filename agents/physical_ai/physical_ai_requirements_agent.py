from typing import Any, Dict

from physical_ai.soft_cpu import resolve_soft_cpu_config
from physical_ai.asic_cpu import resolve_asic_cpu_config
from physical_ai.processor_policy import validate_processor_policy


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = state.get("requirements") if isinstance(state.get("requirements"), dict) else state
    application = str(raw.get("application") or "pmsm_motor_control")
    deployment_architecture = str(raw.get("deployment_architecture") or "automatic")
    processor_policy = raw.get("processor_ip_policy") if isinstance(raw.get("processor_ip_policy"), dict) else {}
    processor_policy = validate_processor_policy(processor_policy)
    implementation_path = str(raw.get("implementation_path") or "digital_ip_asic")
    resolved_deployment = deployment_architecture
    if deployment_architecture == "automatic":
        policy_key = (
            "automatic_fpga_deployment"
            if implementation_path in {"fpga_prototype", "fpga_then_asic"}
            else "automatic_asic_deployment"
        )
        if not processor_policy.get(policy_key):
            raise ValueError(f"Supabase processor_ip_policy.{policy_key} is required")
        resolved_deployment = str(processor_policy[policy_key])
    soft_cpu = resolve_soft_cpu_config(raw.get("soft_cpu_config"), deployment_architecture=resolved_deployment, policy=processor_policy)
    asic_cpu = resolve_asic_cpu_config(raw.get("asic_cpu_config"), deployment_architecture=resolved_deployment, policy=processor_policy)
    requirements = {
        "schema": "chiploop.physical_ai.requirements.v1",
        "application": application,
        "objective": str(raw.get("objective") or "Validate a physical model and create an implementation handoff"),
        "physics_domain": str(raw.get("physics_domain") or "motor_control"),
        "operating_envelope": raw.get("operating_envelope") or {},
        "accuracy": {"maximum_error_percent": float(raw.get("maximum_error_percent") or 3.0)},
        "implementation_target": str(raw.get("implementation_target") or "fpga"),
        "execution_mode": str(raw.get("execution_mode") or "validated"),
        "implementation_path": implementation_path,
        "deployment_architecture_requested": deployment_architecture,
        "deployment_architecture": resolved_deployment,
        "soft_cpu": soft_cpu,
        "asic_cpu": asic_cpu,
        "processor_ip_policy_schema": processor_policy.get("schema"),
        "safety_constraints": list(raw.get("safety_constraints") or []),
        "parameters": dict(raw.get("parameters") or {}),
        "approved": True,
    }
    return {**state, "requirements_contract": requirements}
