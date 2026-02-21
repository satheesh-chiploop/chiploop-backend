import json
from datetime import datetime
from utils.artifact_utils import save_text_artifact_and_record
from llm_router import call_llm  # adjust to your LLM wrapper


def _now():
    return datetime.now().isoformat()


def run_agent(state: dict) -> dict:
    agent_name = "Digital Integration Intent Agent"
    workflow_id = state.get("workflow_id")

    rtl_signatures = state.get("rtl_signatures", {})
    description = state.get("integration_description", "")

    prompt = f"""
You are a digital RTL integration expert.

Available RTL modules and ports:
{json.dumps(rtl_signatures, indent=2)}

User integration description:
{description}

Generate structured JSON with:
- instances
- connections
- tieoffs

Return ONLY valid JSON.
"""

    response = call_llm(prompt)
    intent_json = json.loads(response)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="digital/integrate",
        filename="integration_intent.json",
        content=json.dumps(intent_json, indent=2),
    )

    state["integration_intent"] = intent_json
    state["status"] = "✅ Integration intent generated"
    return state
