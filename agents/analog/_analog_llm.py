import os
import json
from portkey_ai import Portkey
from openai import OpenAI

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY) if PORTKEY_API_KEY else None
client_openai = OpenAI()

DEFAULT_PORTKEY_MODEL = os.getenv("CHIPLOOP_ANALOG_MODEL", "@chiploop/gpt-5-mini")
DEFAULT_OPENAI_MODEL = os.getenv("CHIPLOOP_ANALOG_MODEL_FALLBACK", "gpt-4o-mini")

def safe_json_load(s: str) -> dict:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}

def llm_text(prompt: str) -> str:
    if client_portkey:
        resp = client_portkey.chat.completions.create(
            model=DEFAULT_PORTKEY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        return (resp.choices[0].message.content or "").strip()
    resp = client_openai.chat.completions.create(
        model=DEFAULT_OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()