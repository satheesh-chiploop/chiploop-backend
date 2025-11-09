# imports (top of file)
from datetime import datetime
from portkey_ai import Portkey
from utils.llm_utils import run_llm_fallback  # you already use this
from openai import OpenAI
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

import os
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
portkey = Portkey(api_key=PORTKEY_API_KEY)  # OpenAI-compatible

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
    text = text.strip()
    if not text:
        return []

    resp = client.embeddings.create(
        model="text-embedding-3-small",   # ✅ recommended lightweight model
        input=text
    )

    return resp.data[0].embedding

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
