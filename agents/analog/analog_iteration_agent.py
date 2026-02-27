import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def run_agent(state: dict) -> dict:
    agent_name = "Analog Iteration Proposal Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    # Prefer new correlation delta summary if present
    delta = state.get("analog_delta_summary") or {}
    if not isinstance(delta, dict) or not delta:
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

Rules:
- Patch MUST target model.sv (deterministic artifact).
- No fake precision; propose engineering-level next changes.
- Keep rerun list to dc/ac/tran/psrr/stability as needed.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)
    if not isinstance(obj, dict):
        obj = {}

    rationale = (obj.get("iteration_rationale_md") or "").strip() or "# Iteration Rationale\n\n(TBD)\n"
    plan = obj.get("next_run_plan") if isinstance(obj.get("next_run_plan"), dict) else {"changes": [], "rerun": []}
    diff = (obj.get("patch_diff") or "").strip()

    # Enforce patch target safety
    if diff and "a/model.sv" not in diff:
        # If model.sv isn't referenced, blank the diff rather than emitting wrong file diffs
        diff = ""

    state["analog_next_run_plan"] = plan

    if not preview_only:
        # Canonical scaffold outputs
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration/iteration_proposal.md", rationale)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration/backlog.yaml",
                                      "\n".join([f"- {c}" for c in (plan.get("changes") or [])]) + "\n")
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration/next_run_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration/iteration_patch.diff", diff or "")

        # Legacy compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration_rationale.md", rationale)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "next_run_plan.json", json.dumps(plan, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "iteration_patch.diff", diff or "")

    return state