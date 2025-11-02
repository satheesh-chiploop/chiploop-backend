# backend/planner/ranking.py
import hashlib
from .capability_graph import extract_niches, apply_niche_bias

def _context_hash(struct):
    return hashlib.sha256(str(struct).encode("utf-8")).hexdigest()[:12]

def _semantic_score(agent_name, struct):
    """
    Simple lexical grounding. Later → replace with embeddings.
    """
    text = str(struct).lower()
    score = 0
    if agent_name.lower() in text:
        score += 0.4
    if "power" in text and "power" in agent_name.lower():
        score += 0.6
    if "cdc" in text and "cdc" in agent_name.lower():
        score += 0.6
    return score

def apply_exploration_boost(scores, struct, memory_lookup):
    """
    Update-Triggered Exploration Boost:
    If context hash is new → boost lower-ranked options.
    """
    ctx = _context_hash(struct)
    seen = memory_lookup(ctx)

    boosted = []
    if not seen:  # new context = explore
        for i, (agent, score) in enumerate(scores):
            score += (0.15 * (1 - i / len(scores)))  # small exploration boost
            boosted.append((agent, score))
        return boosted
    return scores

def rank_candidates(struct, candidates, memory_lookup):
    # 1) Base semantic scores
    scores = [(agent, _semantic_score(agent, struct)) for agent in candidates]

    # 2) Apply niche fit bias
    niches = extract_niches(struct)
    scores = apply_niche_bias(scores, niches)

    # 3) Apply exploration boost if context is new
    scores = apply_exploration_boost(scores, struct, memory_lookup)

    # Sort descending
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores
