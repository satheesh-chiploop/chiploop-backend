import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text

def run_agent(state: dict) -> dict:
    agent_name = "Analog Netlist Scaffold Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are an analog circuit designer.

Given this JSON spec:
{json.dumps(spec, indent=2)}

Generate a SPICE netlist scaffold as plain text (not markdown).
Requirements:
- Include .SUBCKT with pin order matching supplies + pins listed in spec.
- Include placeholder devices and comments for blocks (input stage, bias, pass device, compensation, etc.).
- Include .ENDS
Return ONLY netlist text.
"""

    netlist = llm_text(prompt)
    state["analog_netlist"] = netlist

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="netlist.sp",
            content=netlist,
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="netlist_summary.md",
            content=f"# Netlist Scaffold Summary\n\nGenerated a SPICE scaffold for: {(spec.get('block') or {}).get('name','(unknown)')}\n",
        )

    return state