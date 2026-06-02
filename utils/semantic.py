# imports (top of file)
from datetime import datetime
from utils.llm_utils import run_llm_fallback  # you already use this
from model_gateway import embed_text
import os

async def summarize_capability_long(description: str) -> str:
    """
    Produce a concise 2–4 sentence capability summary (no code/impl details),
    focused on purpose, inputs/outputs, and when to use this agent.
    """
    prompt = f"""
You are documenting a hardware-design agent capability.

Task: Summarize its functional behavior and role in 2–4 short sentences.
Rules: Focus on purpose, inputs/outputs, and typical usage. Avoid implementation or code details.

Description:
{description}
"""
    text = await run_llm_fallback(prompt)
    return (text or "").strip()

async def compute_embedding(text: str) -> list[float]:
    """
    Compute embedding vector using OpenAI directly (not Portkey).
    This ensures compatibility with pgvector and avoids Portkey headers.
    """
    return embed_text(text)

def build_capability_signature(agent: dict) -> str:
    name = agent.get("agent_name", "")
    desc = agent.get("description", "")
    tags = ", ".join(agent.get("capability_tags", []) or [])
    io = agent.get("interfaces", "")  # if exists
    purpose = agent.get("purpose", "")

    signature = f"""
Agent Name: {name}
Purpose: {desc or purpose}
Capabilities: {tags}
Interfaces / IO Behavior: {io}

This agent is used when workflows require the above capabilities.
"""
    return signature.strip()
