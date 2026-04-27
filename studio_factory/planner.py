from typing import Dict, List, Tuple

from .generator import STANDARD_HOOKS, build_agent_spec, build_generated_files, build_registry_patch_plan, slugify
from .models import AgentFactoryPlan, AgentFactoryRequest
from .registry_lookup import lookup_existing_agents


def _resolve_skills(request: AgentFactoryRequest, registry) -> Tuple[List[str], List[Dict]]:
    existing = set(registry.skills.keys())
    requested = list(request.required_skills)
    if not requested:
        text = f"{request.name} {request.natural_language_request}".lower()
        if "sta" in text or "constraint" in text:
            requested.append("sta_constraint_generation")
        if "rtl" in text:
            requested.append("rtl_generation")
        requested.append("artifact_publish")

    proposed = []
    resolved = []
    for skill in sorted(set(requested)):
        if skill in existing:
            resolved.append(skill)
        else:
            proposed.append(
                {
                    "name": skill,
                    "description": f"TODO: Review generated skill for {skill}.",
                    "domains": [request.domain or request.loop_type or "system"],
                    "tools": request.required_tools or ["python"],
                }
            )
            resolved.append(skill)
    return resolved, proposed


def _resolve_tools(request: AgentFactoryRequest, registry, skills: List[str]) -> List[str]:
    tools = set(request.required_tools or ["python", "supabase"])
    for skill in skills:
        spec = registry.skills.get(skill)
        if spec:
            tools.update(spec.tools)
    return sorted(tool for tool in tools if tool in registry.tools)


def plan_factory_request(request: AgentFactoryRequest, registry_dir: str = "registry") -> AgentFactoryPlan:
    registry, lookup = lookup_existing_agents(request, registry_dir=registry_dir)

    if lookup.exact_matches:
        match = lookup.exact_matches[0]
        return AgentFactoryPlan(
            decision="reuse_existing",
            proposed_agent_spec={},
            validation_checklist=["Exact registry match found; no generation required."],
            risk_notes=["Factory stopped before generation by exact-match guard."],
            exact_match=match.name,
        )

    if lookup.similar_matches and not request.allow_extension:
        return AgentFactoryPlan(
            decision="extend_or_create_variant",
            proposed_agent_spec={},
            validation_checklist=["Similar matches found; rerun with allow_extension=true to generate a variant."],
            risk_notes=["Generation blocked to avoid duplicating existing capability."],
            similar_matches=[m.name for m in lookup.similar_matches],
        )

    skills, proposed_skills = _resolve_skills(request, registry)
    tools = _resolve_tools(request, registry, skills)
    agent_spec = build_agent_spec(request, skills, tools)
    files = build_generated_files(request, agent_spec, proposed_skills)
    patch = build_registry_patch_plan(request, agent_spec, proposed_skills)
    decision = "create_new" if not lookup.similar_matches else "create_new_variant"

    return AgentFactoryPlan(
        decision=decision,
        proposed_agent_spec=agent_spec,
        proposed_skill_specs=proposed_skills,
        proposed_tool_refs=tools,
        proposed_hook_refs=STANDARD_HOOKS,
        files_to_generate=files,
        registry_patch=patch,
        validation_checklist=[
            "Review generated agent stub before promotion.",
            "Run generated tests manually after implementation.",
            "Dry-run registry patch before merging.",
            f"Generated module slug: {slugify(request.name)}",
        ],
        risk_notes=[
            "Generated files are review artifacts only.",
            "Factory never writes into production agents/, registry/, or main.py.",
        ],
        similar_matches=[m.name for m in lookup.similar_matches],
    )
