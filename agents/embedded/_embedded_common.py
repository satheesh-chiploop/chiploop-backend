import os
from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY) if PORTKEY_API_KEY else None
client_openai = OpenAI()

def get_embedded_model() -> str:
    return (
        os.getenv("CHIPLOOP_EMBEDDED_MODEL")
        or os.getenv("CHIPLOOP_FIRMWARE_MODEL")
        or os.getenv("CHIPLOOP_DEFAULT_MODEL")
        or "@chiploop/gpt-4o-mini"
    )

def llm_chat(prompt: str, system: str = "") -> str:
    model = get_embedded_model()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Prefer Portkey when available (matches your other agents pattern)
    if client_portkey:
        resp = client_portkey.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
        )
        return (resp.choices[0].message.content or "").strip()

    resp = client_openai.chat.completions.create(
        model=model,
        messages=messages,
    )
    return (resp.choices[0].message.content or "").strip()

def ensure_workflow_dir(state: dict) -> str:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    os.makedirs(workflow_dir, exist_ok=True)
    state["workflow_dir"] = workflow_dir
    return workflow_dir

def write_artifact(state: dict, rel_path: str, content: str, key: str | None = None) -> str:
    # rel_path is stored under workflow_dir via artifact_utils
    key = key or os.path.basename(rel_path)
    save_text_artifact_and_record(state, rel_path, content, key=key)
    return rel_path
