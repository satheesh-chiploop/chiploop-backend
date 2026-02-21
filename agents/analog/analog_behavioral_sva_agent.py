import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text

def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Assertions Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are a mixed-signal verification engineer.

Create SystemVerilog assertions/checkers (plain text) to validate:
- enable sequencing
- output stays within min/max when enabled
- settling time window check (use parameter placeholders)
- monotonic response check placeholder for dc sweep

Use spec:
{json.dumps(spec, indent=2)}

Return ONLY code (no markdown). Keep it modular as "analog_checks" package or include file.
"""

    sva = llm_text(prompt)
    state["analog_sva"] = sva

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="sva.sv",
            content=sva,
        )

    return state