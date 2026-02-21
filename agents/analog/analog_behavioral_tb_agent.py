import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text

def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Testbench Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    sim_plan = state.get("analog_sim_plan") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are a SystemVerilog RNM verification engineer.

Generate a minimal SystemVerilog testbench (plain text, not markdown) for the analog behavioral model.
Use scenarios and sweeps from this sim plan if present:
{json.dumps(sim_plan, indent=2)}

Use spec:
{json.dumps(spec, indent=2)}

Output: full tb code, compile-friendly.
Include:
- clock (if any digital pins)
- enable sequencing
- basic stimulus: DC sweep loop + transient step
- dump/print key metrics placeholders
Return ONLY code.
"""

    tb = llm_text(prompt)
    state["analog_tb"] = tb

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="tb.sv",
            content=tb,
        )

    return state