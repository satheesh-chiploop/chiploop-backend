import re
from typing import Iterable, List, Sequence, Set

from studio_contract.registry import StudioRegistry
from studio_contract.specs import AgentSpec

from .models import AgentPlanRequest, ExistingAgentMatch


STOPWORDS = {
    "a",
    "an",
    "agent",
    "and",
    "build",
    "create",
    "for",
    "generate",
    "make",
    "new",
    "of",
    "the",
    "to",
    "with",
}


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def tokens(value: str) -> Set[str]:
    return {t for t in normalize_text(value).split() if t and t not in STOPWORDS}


def _overlap(left: Iterable[str], right: Iterable[str]) -> Set[str]:
    return set(left) & set(right)


def _score_agent(agent: AgentSpec, request: AgentPlanRequest) -> ExistingAgentMatch:
    request_text = request.natural_language_request or ""
    req_tokens = tokens(" ".join([request_text, *(request.inputs or []), *(request.outputs or [])]))
    name_tokens = tokens(agent.name)
    desc_tokens = tokens(agent.description)
    skill_tokens = {token for skill in agent.required_skills for token in tokens(skill)}

    score = 0.0
    reasons: List[str] = []

    if normalize_text(agent.name) == normalize_text(request_text):
        score += 1.0
        reasons.append("normalized_name_exact")

    name_overlap = _overlap(req_tokens, name_tokens)
    if name_overlap:
        score += min(0.35, 0.08 * len(name_overlap))
        reasons.append(f"name_token_overlap:{','.join(sorted(name_overlap))}")

    desc_overlap = _overlap(req_tokens, desc_tokens)
    if desc_overlap:
        score += min(0.25, 0.04 * len(desc_overlap))
        reasons.append("description_keyword_overlap")

    skill_overlap = _overlap(req_tokens, skill_tokens)
    if skill_overlap:
        score += min(0.2, 0.06 * len(skill_overlap))
        reasons.append(f"skill_overlap:{','.join(sorted(skill_overlap))}")

    if request.domain and agent.domain == request.domain:
        score += 0.15
        reasons.append("domain_match")
    elif request.domain and agent.domain != request.domain:
        score -= 0.1

    if request.loop_type and agent.loop_type == request.loop_type:
        score += 0.15
        reasons.append("loop_type_match")
    elif request.loop_type and agent.loop_type != request.loop_type:
        score -= 0.1

    io_overlap = _overlap(req_tokens, tokens(" ".join(agent.inputs + agent.outputs)))
    if io_overlap:
        score += min(0.15, 0.03 * len(io_overlap))
        reasons.append("input_output_overlap")

    score = max(0.0, min(1.0, score))
    return ExistingAgentMatch(
        name=agent.name,
        score=round(score, 4),
        match_reasons=reasons,
        loop_type=agent.loop_type,
        domain=agent.domain,
        entrypoint=agent.entrypoint,
    )


def match_agents(
    registry: StudioRegistry,
    request: AgentPlanRequest,
    *,
    exact_threshold: float = 0.92,
    similar_threshold: float = 0.25,
    limit: int = 8,
) -> Sequence[ExistingAgentMatch]:
    scored = [_score_agent(agent, request) for agent in registry.agents.values()]
    matches = [m for m in scored if m.score >= similar_threshold]
    matches.sort(key=lambda item: (-item.score, item.name))

    exact = [m for m in matches if m.score >= exact_threshold]
    if exact:
        return exact[:limit]
    return matches[:limit]


def split_exact_similar(matches: Sequence[ExistingAgentMatch], exact_threshold: float = 0.92):
    exact = [m for m in matches if m.score >= exact_threshold]
    similar = [m for m in matches if m.score < exact_threshold]
    return exact, similar
