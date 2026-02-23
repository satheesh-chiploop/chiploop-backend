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

def _infer_agent_name(state: dict) -> str:
    # Try a few common keys that your executor may set.
    return (
        state.get("current_agent")
        or state.get("agent_name")
        or state.get("step")
        or "Embedded Agent"
    )

def write_artifact(state: dict, rel_path: str, content: str, key: str | None = None) -> str:
    """
    Record artifacts the same way as Validation agents so:
    - workflows.artifacts gets updated
    - /download_zip works
    """
    workflow_id = state.get("workflow_id")
    if not workflow_id:
        raise RuntimeError("Missing workflow_id in state (required for artifact recording).")

    # rel_path examples: "firmware/register_map.json", "firmware/hal/lib.rs"
    # We'll store under subdir="firmware" and filename="register_map.json" etc.
    parts = rel_path.split("/", 1)
    if len(parts) == 1:
        subdir = "firmware"
        filename = parts[0]
    else:
        subdir = parts[0]
        filename = parts[1]  # keep nested path under subdir if your artifact_utils supports it

    agent_name = _infer_agent_name(state)

    # If artifact_utils expects a flat filename, keep only basename:
    # filename = os.path.basename(filename)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir=subdir,
        filename=filename,
        content=content,
    )
    return rel_path


