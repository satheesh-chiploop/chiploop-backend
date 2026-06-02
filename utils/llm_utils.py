import json
import os

import httpx
from loguru import logger

from model_gateway import complete_text


USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")


async def run_llm_fallback(prompt: str) -> str:
    """
    Shared LLM runner.

    Hosted SaaS defaults to the ChipLoop model gateway, which currently routes to
    OpenAI gpt-5.5. Customers can override routing with CHIPLOOP_MODEL_PROFILE_PATH.
    Ollama remains as an explicit local override for developer/private testing.
    """
    if USE_LOCAL_OLLAMA:
        try:
            full_text = ""
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    OLLAMA_URL,
                    json={"model": os.getenv("OLLAMA_MODEL", "llama3"), "prompt": prompt, "stream": True},
                ) as response:
                    async for chunk in response.aiter_text():
                        for line in chunk.splitlines():
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if "response" in data:
                                full_text += data["response"]
                            if data.get("done"):
                                return full_text.strip()
            return full_text.strip()
        except Exception as exc:
            logger.warning(f"Ollama fallback failed: {exc}")

    try:
        return complete_text(prompt, capability="default", temperature=0.3)
    except Exception as exc:
        logger.error(f"ChipLoop model gateway failed: {exc}")
        return ""
