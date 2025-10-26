import os
import json
import httpx
import requests
from loguru import logger
from openai import OpenAI

# --- Environment configuration ---
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
from portkey_ai import Portkey
from openai import OpenAI

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


async def run_llm_fallback(prompt: str) -> str:
    """
    Multi-backend LLM runner with robust fallback chain:
    1. Ollama local streaming (preferred if enabled)
    2. Portkey API
    3. OpenAI (final fallback)
    Returns clean text output aggregated from model response.
    """
    logger.info("🧠 run_llm_fallback: initiating multi-backend inference...")

    # --- 1️⃣ Try local Ollama (streaming) ---
    if USE_LOCAL_OLLAMA:
        try:
            full_text = ""
            async with httpx.AsyncClient(timeout=120.0) as client:
                logger.info(f"🚀 Using Ollama model at {OLLAMA_URL}")
                async with client.stream(
                    "POST",
                    OLLAMA_URL,
                    json={"model": "llama3", "prompt": prompt, "stream": True},
                ) as response:
                    async for chunk in response.aiter_text():
                        for line in chunk.splitlines():
                            try:
                                data = json.loads(line)
                                if "response" in data:
                                    full_text += data["response"]
                                if data.get("done"):
                                    logger.info("✅ Ollama stream complete.")
                                    return full_text.strip()
                            except json.JSONDecodeError:
                                continue
            return full_text.strip() or ""
        except Exception as e:
            logger.warning(f"⚠️ Ollama failed: {e}")

    # --- 2️⃣ Try Portkey ---
    if PORTKEY_API_KEY:
        try:
            logger.info("🌐 Using Portkey SDK backend...")

            completion = client_portkey.chat.completions.create(
               model="@chiploop/gpt-4o-mini",  # or "@chiploop/gpt-4o-mini"
               messages=[{"role": "user", "content": prompt}],
               temperature=1,
               stream=False,
            )

            content = completion.choices[0].message.content or ""
            logger.info("✅ Portkey inference successful.")
            return content

        except Exception as e:
            logger.warning(f"⚠️ Portkey SDK failed: {e}")

    # --- 3️⃣ Fallback to OpenAI ---
    if OPENAI_API_KEY:
        try:
            logger.info("🤖 Falling back to OpenAI backend...")
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            result = resp.choices[0].message.content
            logger.info("✅ OpenAI inference successful.")
            return result
        except Exception as e:
            logger.error(f"❌ OpenAI failed: {e}")

    logger.error("❌ All LLM backends failed in run_llm_fallback.")
    return ""
