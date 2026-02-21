import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Iteration Proposal Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    delta = state.get("analog_delta_summary") or {}
    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    prompt = f"""
You are improving an analog behavioral model based on correlation deltas.

Given delta summary:
{json.dumps(delta, indent=2)}

Return ONLY JSON:
{{
  "iteration_rationale_md": "# Iteration Rationale\\n...",
  "next_run_plan": {{"changes":["..."],"rerun":["dc","ac","tran"]}},
  "patch_diff": "diff --git a/model.sv b/model.sv\\n..."
}}
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)

    rationale = (obj.get("iteration_rationale_md") or "").strip()
    plan = obj.get("next_run_plan") or {}
    diff = (obj.get("patch_diff") or "").strip()

    state["analog_next_run_plan"] = plan

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="iteration_rationale.md",
            content=rationale or "# Iteration Rationale\n\n(TBD)\n",
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="next_run_plan.json",
            content=json.dumps(plan, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="iteration_patch.diff",
            content=diff or "",
        )

    return state