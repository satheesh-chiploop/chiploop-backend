import os, requests
from loguru import logger
from openai import OpenAI

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
PORTKEY_BASE_URL = os.getenv("PORTKEY_BASE_URL", "https://api.portkey.ai")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def run_llm_fallback(prompt: str) -> str:
    """
    Runs a simple text-generation LLM chain with fallbacks:
    1. Ollama local (if available)
    2. Portkey API (if configured)
    3. OpenAI (final fallback)
    """
    logger.info("🧠 run_llm_fallback: initiating multi-backend inference...")

    # --- Ollama ---
    if USE_LOCAL_OLLAMA:
        try:
            payload = {"model": "llama3", "prompt": prompt}
            r = requests.post(OLLAMA_URL, json=payload, timeout=120)
            return r.text
        except Exception as e:
            logger.warning(f"Ollama failed: {e}")

    # --- Portkey ---
    if PORTKEY_API_KEY:
        try:
            headers = {
                "x-portkey-api-key": PORTKEY_API_KEY,
                "Content-Type": "application/json",
            }
            body = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }
            r = requests.post(f"{PORTKEY_BASE_URL}/v1/chat/completions", json=body, headers=headers, timeout=60)
            content = r.json()["choices"][0]["message"]["content"]
            return content
        except Exception as e:
            logger.warning(f"Portkey failed: {e}")

    # --- OpenAI ---
    if OPENAI_API_KEY:
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI failed: {e}")

    logger.error("❌ All LLM backends failed in run_llm_fallback.")
    return "[]"
