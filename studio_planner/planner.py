from typing import List, Set

from studio_contract.registry import load_registry

from .models import AgentPlanRequest, AgentPlanResult
from .registry_matcher import match_agents, split_exact_similar, tokens


def _reusable_skills(request: AgentPlanRequest, registry) -> List[str]:
    req_tokens = tokens(request.natural_language_request)
    out = []
    for skill in registry.skills.values():
        skill_tokens = tokens(" ".join([skill.name, skill.description, *skill.domains]))
        if req_tokens & skill_tokens:
            out.append(skill.name)
    return sorted(set(out))


def _tools_for_skills(skills: List[str], registry) -> List[str]:
    tools: Set[str] = set()
    for skill_name in skills:
        skill = registry.skills.get(skill_name)
        if skill:
            tools.update(skill.tools)
    return sorted(t for t in tools if t in registry.tools)


def _recommendation(exact, similar, reusable_skills):
    if exact:
        return "reuse_existing"
    if len(similar) >= 2:
        return "compose_existing"
    if similar and reusable_skills:
        return "extend_existing"
    return "create_new"


def plan_agent(request: AgentPlanRequest, registry_dir: str = "registry") -> AgentPlanResult:
    registry = load_registry(registry_dir)
    matches = match_agents(registry, request)
    exact, similar = split_exact_similar(matches)
    skills = _reusable_skills(request, registry)
    tools = _tools_for_skills(skills, registry)
    hooks = sorted(registry.hooks.keys())
    recommendation = _recommendation(exact, similar, skills)
    confidence = 0.0
    if exact:
        confidence = exact[0].score
    elif similar:
        confidence = min(0.85, similar[0].score)
    elif skills:
        confidence = 0.35

    missing = []
    if recommendation == "create_new":
        missing.append(request.natural_language_request)

    explanation = (
        "Exact registry match found; reuse existing agent."
        if exact
        else "Similar agents and reusable Studio skills found."
        if similar
        else "No suitable existing agent found; plan a new generated agent."
    )

    return AgentPlanResult(
        requested_capability=request.natural_language_request,
        exact_matches=exact,
        similar_matches=similar,
        reusable_skills=skills,
        reusable_tools=tools,
        reusable_hooks=hooks,
        missing_capabilities=missing,
        recommendation=recommendation,
        confidence_score=round(confidence, 4),
        explanation=explanation,
    )
