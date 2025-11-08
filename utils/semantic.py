# imports (top of file)
from datetime import datetime
from portkey_ai import Portkey
from utils.llm_utils import run_llm_fallback  # you already use this

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
    """Return embedding vector from OpenAI via Portkey (text-embedding-3-small)."""
    resp = portkey.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return resp.data[0].embedding  # list[float]
