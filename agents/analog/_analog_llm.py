import os
import json
from model_gateway import complete_text

DEFAULT_ANALOG_CAPABILITY = os.getenv("CHIPLOOP_ANALOG_MODEL_CAPABILITY", "analog_generation")

def safe_json_load(s: str) -> dict:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}

def llm_text(prompt: str) -> str:
    return complete_text(prompt, capability=DEFAULT_ANALOG_CAPABILITY)
