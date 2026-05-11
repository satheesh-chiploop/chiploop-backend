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
    return sorted(tools)


def _proposed_tool_specs(tools: List[str], registry, request: AgentFactoryRequest) -> List[Dict]:
    existing = set(registry.tools.keys())
    return [
        {
            "name": tool,
            "description": f"Generated tool/MCP spec for {tool}. Review connection details before marking runnable.",
            "kind": "mcp" if "mcp" in tool.lower() else "service",
            "optional": True,
            "metadata": {
                "generated_by": "studio_factory_v1",
                "review_required": True,
                "domain": request.domain or request.loop_type or "system",
            },
        }
        for tool in tools
        if tool not in existing
    ]


def _resolve_hooks(request: AgentFactoryRequest, registry) -> Tuple[List[str], List[Dict]]:
    requested = list(dict.fromkeys((request.required_hooks or []) + STANDARD_HOOKS))
    existing = set(registry.hooks.keys())
    proposed = []
    for hook in requested:
        if hook in existing:
            continue
        lifecycle = "pre_run" if hook.startswith("pre_") else "post_run" if hook.startswith("post_") else "on_failure" if hook.startswith("on_failure") else "custom"
        proposed.append(
            {
                "name": hook,
                "description": f"Generated lifecycle hook spec for {hook}. Review implementation before marking runnable.",
                "lifecycle": lifecycle,
                "metadata": {
                    "generated_by": "studio_factory_v1",
                    "review_required": True,
                },
            }
        )
    return requested, proposed


def plan_factory_request(request: AgentFactoryRequest, registry_dir: str = "registry") -> AgentFactoryPlan:
    registry, lookup = lookup_existing_agents(request, registry_dir=registry_dir)

    if lookup.exact_matches and not request.force_create_private:
        match = lookup.exact_matches[0]
        return AgentFactoryPlan(
            decision="reuse_existing",
            proposed_agent_spec={},
            validation_checklist=["Exact registry match found; no generation required."],
            risk_notes=["Factory stopped before generation by exact-match guard."],
            exact_match=match.name,
        )

    if lookup.similar_matches and not request.allow_extension and not request.force_create_private:
        return AgentFactoryPlan(
            decision="extend_or_create_variant",
            proposed_agent_spec={},
            validation_checklist=["Similar matches found; rerun with allow_extension=true to generate a variant."],
            risk_notes=["Generation blocked to avoid duplicating existing capability."],
            similar_matches=[m.name for m in lookup.similar_matches],
        )

    skills, proposed_skills = _resolve_skills(request, registry)
    tools = _resolve_tools(request, registry, skills)
    hooks, proposed_hooks = _resolve_hooks(request, registry)
    proposed_tools = _proposed_tool_specs(tools, registry, request)
    request.required_hooks = hooks
    agent_spec = build_agent_spec(request, skills, tools)
    files = build_generated_files(request, agent_spec, proposed_skills, proposed_tools, proposed_hooks)
    patch = build_registry_patch_plan(request, agent_spec, proposed_skills, proposed_tools, proposed_hooks)
    decision = "create_new" if not lookup.similar_matches else "create_new_variant"
    risk_notes = [
        "Generated files are review artifacts only.",
        "Factory never writes into production agents/, registry/, or main.py.",
    ]
    if request.force_create_private and (lookup.exact_matches or lookup.similar_matches):
        risk_notes.append("Private creation requested; existing matches are references and did not block this draft.")

    return AgentFactoryPlan(
        decision=decision,
        proposed_agent_spec=agent_spec,
        proposed_skill_specs=proposed_skills,
        proposed_tool_refs=tools,
        proposed_tool_specs=proposed_tools,
        proposed_hook_refs=hooks,
        proposed_hook_specs=proposed_hooks,
        files_to_generate=files,
        registry_patch=patch,
        validation_checklist=[
            "Review generated agent stub before promotion.",
            "Run generated tests manually after implementation.",
            "Dry-run registry patch before merging.",
            f"Generated module slug: {slugify(request.name)}",
        ],
        risk_notes=risk_notes,
        exact_match=lookup.exact_matches[0].name if lookup.exact_matches else None,
        similar_matches=[m.name for m in lookup.similar_matches],
    )
