import re
from typing import Any

from .content import HELP_TOPICS


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def _topic_score(topic: dict[str, Any], question_tokens: set[str], question: str) -> int:
    haystack = " ".join(
        [
            topic["title"],
            topic["category"],
            topic["summary"],
            " ".join(topic["body"]),
            " ".join(topic["keywords"]),
        ]
    ).lower()
    score = 0
    for token in question_tokens:
        if token in haystack:
            score += 1
    for keyword in topic["keywords"]:
        keyword_lower = keyword.lower()
        if keyword_lower in question:
            score += 3
    return score


def _best_topics(question: str) -> list[dict[str, Any]]:
    normalized = question.lower().strip()
    question_tokens = _tokens(normalized)
    ranked = sorted(
        HELP_TOPICS,
        key=lambda topic: _topic_score(topic, question_tokens, normalized),
        reverse=True,
    )
    matched = [topic for topic in ranked if _topic_score(topic, question_tokens, normalized) > 0]
    return (matched or ranked)[:3]


def answer_help_question(question: str) -> dict[str, Any]:
    cleaned = question.strip()
    if len(cleaned) < 3:
        raise ValueError("question_too_short")

    topics = _best_topics(cleaned)
    primary = topics[0]
    related = topics[1:]
    answer_parts = [
        primary["summary"],
        primary["body"][0],
    ]
    if related:
        answer_parts.append(
            "Related areas to check: "
            + ", ".join(topic["title"] for topic in related)
            + "."
        )

    return {
        "status": "ok",
        "answer": " ".join(answer_parts),
        "suggested_actions": primary["actions"],
        "sources": [
            {
                "slug": topic["slug"],
                "title": topic["title"],
                "category": topic["category"],
                "href": f"/help?topic={topic['slug']}",
            }
            for topic in topics
        ],
    }
