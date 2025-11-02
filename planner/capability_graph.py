# backend/planner/capability_graph.py
from agent_capabilities import AGENT_CAPABILITIES

def extract_niches(struct):
    """Return niche attributes inferred from structured_spec_final."""
    niches = []
    if not struct:
        return niches

    # Multi power domain niche
    if isinstance(struct.get("power_domains"), list) and len(struct["power_domains"]) > 1:
        niches.append("multi_power_domain")

    # CDC niche
    if struct.get("cdc_crossings"):
        niches.append("cdc_heavy")

    # PDC niche
    if struct.get("pdc_crossings"):
        niches.append("pdc_heavy")

    # Compute-heavy niche (simple heuristic: many ops or arithmetic module type)
    text = str(struct).lower()
    if any(op in text for op in ["add", "mul", "pipeline", "compute"]):
        niches.append("compute_heavy")

    return niches


def get_candidate_agents(structured_spec_final):
    """
    Return list of agents that *could* be used for this workflow,
    purely based on capability tags and domain = digital.
    (This does NOT rank or choose — it provides the pool.)
    """
    candidates = []
    for agent_name, meta in AGENT_CAPABILITIES.items():
        if meta.get("domain") == "digital":
            candidates.append(agent_name)

    return candidates


def apply_niche_bias(scores, niches):
    """
    scores: [(agent_name, score)]
    niches: ["multi_power_domain", "cdc_heavy", ...]
    Boost score based on niche fit.
    """
    niche_boosts = {
        "multi_power_domain": ["Power Intent Agent"],
        "cdc_heavy": ["CDC Guard Agent"],
        "pdc_heavy": ["PDC Guard Agent"],
        "compute_heavy": []
    }

    boosted = []
    for agent, score in scores:
        for niche in niches:
            if agent in niche_boosts.get(niche, []):
                score += 0.25  # niche fit bonus
        boosted.append((agent, score))
    return boosted
