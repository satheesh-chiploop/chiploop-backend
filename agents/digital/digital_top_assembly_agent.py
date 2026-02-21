import os
import json
from utils.artifact_utils import save_text_artifact_and_record


def run_agent(state: dict) -> dict:
    agent_name = "Digital Top Assembly Agent"
    workflow_id = state.get("workflow_id")
    workflow_dir = state.get("workflow_dir")
    top_name = state.get("top_module")
    intent = state.get("integration_intent", {})

    lines = []
    lines.append(f"module {top_name} ();")
    lines.append("")

    for inst in intent.get("instances", []):
        lines.append(f"    {inst['module']} {inst['name']} ();")

    lines.append("")
    lines.append("endmodule")

    top_code = "\n".join(lines)

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "digital/integrate",
        f"{top_name}.sv",
        top_code,
    )

    state["status"] = "✅ Top module generated"
    return state
