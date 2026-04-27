from studio_contract.registry import load_registry
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent

from .models import AgentFactoryRequest


def lookup_existing_agents(request: AgentFactoryRequest, registry_dir: str = "registry"):
    registry = load_registry(registry_dir)
    plan = plan_agent(
        AgentPlanRequest(
            natural_language_request=request.name or request.natural_language_request,
            domain=request.domain,
            loop_type=request.loop_type,
            inputs=request.inputs,
            outputs=request.outputs,
        ),
        registry_dir=registry_dir,
    )
    return registry, plan
