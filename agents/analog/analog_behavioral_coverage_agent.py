import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Coverage Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are defining coverage for analog behavioral validation.
Coverage here means:
- scenario coverage (modes, enable/disable, load_step, fault)
- corner coverage (vdd/temp/process)
- sweep coverage classes (dc/ac/tran)
Return ONLY JSON:
{{
  "scenario_coverage": ["..."],
  "corner_coverage": {{"vdd":[], "temp_c":[], "process":[]}},
  "sweep_coverage": ["dc","ac","tran"],
  "notes": ["..."]
}}
Use spec:
{json.dumps(spec, indent=2)}
"""

    out = llm_text(prompt)
    cov = safe_json_load(out)
    state["analog_coverage_plan"] = cov

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="coverage_plan.json",
            content=json.dumps(cov, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="validation_summary.md",
            content="# Validation Coverage Summary\n\n" + json.dumps(cov, indent=2),
        )

    return state