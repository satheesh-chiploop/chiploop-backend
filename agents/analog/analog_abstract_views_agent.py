import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Abstract Views Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are creating integration abstracts for an analog macro.

Using this spec:
{json.dumps(spec, indent=2)}

Return ONLY JSON:
{{
  "lef": "VERSION 5.8 ;\\nMACRO ...",
  "lib_stub": "library(...) {{ ... }}",
  "integration_notes_md": "# Integration Notes\\n..."
}}
Rules:
- LEF should be minimal: MACRO, SIZE placeholder, PINs for supplies/digital/analog pins.
- LIB stub only if digital pins exist; else keep it as an empty string.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)
    lef = (obj.get("lef") or "").strip()
    lib_stub = (obj.get("lib_stub") or "").strip()
    notes = (obj.get("integration_notes_md") or "").strip()

    if not preview_only:
        save_text_artifact_and_record(workflow_id=workflow_id, agent_name=agent_name, subdir="analog/abstracts", filename="macro.lef", content=lef or "")
        save_text_artifact_and_record(workflow_id=workflow_id, agent_name=agent_name, subdir="analog/abstracts", filename="macro_stub.lib", content=lib_stub or "")
        save_text_artifact_and_record(workflow_id=workflow_id, agent_name=agent_name, subdir="analog/abstracts", filename="integration_notes.md", content=notes or "# Integration Notes\n\n(TBD)\n")

    return state