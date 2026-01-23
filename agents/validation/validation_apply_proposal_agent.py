# backend/agents/validation/validation_apply_proposal_agent.py

from utils.artifact_utils import save_text_artifact_and_record


def run_agent(state: dict) -> dict:
    """
    WF9: Apply Proposal Agent (MVP-safe)

    Deterministic DB mutation:
      - resolve base plan (by id or name)
      - load proposed plan (state dict OR proposal artifact)
      - insert vNext plan as active
      - deactivate prior active plan(s) for same user+name
      - write artifacts:
          - validation/apply_status.md (success)
          - validation/apply_no_action.md (missing inputs)
          - validation/apply_error.md (exception)
    """
    workflow_id = state.get("workflow_id")
    supabase = state.get("supabase_client")

    if not workflow_id:
        state["status"] = "❌ Validation Apply Proposal: missing workflow_id"
        return state
    if supabase is None:
        state["status"] = "❌ Validation Apply Proposal: missing supabase_client in state"
        return state

    # Normalize MVP keys
    if state.get("validation_test_plan_name") and not state.get("test_plan_name"):
        state["test_plan_name"] = state.get("validation_test_plan_name")

    # Must have user_id for plan resolution and row insert
    if not state.get("user_id"):
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Apply Proposal Agent",
            subdir="validation",
            filename="apply_no_action.md",
            content=(
                "# Apply Proposal\n\n"
                "No plan applied because `user_id` is missing from state.\n"
            ),
        )
        state["status"] = "🟡 WF9 no-op: missing user_id"
        return state

    # Must have proposal payload or proposal artifact path
    has_proposed = isinstance(state.get("proposed_test_plan"), dict) and bool(state.get("proposed_test_plan"))
    has_artifact = bool(state.get("proposal_artifact_path") or state.get("source_artifact_path") or state.get("proposed_test_plan_artifact_path"))
    if not has_proposed and not has_artifact:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Apply Proposal Agent",
            subdir="validation",
            filename="apply_no_action.md",
            content=(
                "# Apply Proposal\n\n"
                "No plan applied because no proposal was provided.\n\n"
                "Expected one of:\n"
                "- `state.proposed_test_plan` (dict)\n"
                "- `state.proposal_artifact_path` (storage key)\n"
            ),
        )
        state["status"] = "🟡 WF9 no-op: missing proposal"
        return state

    from services.validation.validation_apply_proposal_service import apply_test_plan_proposal

    try:
        return apply_test_plan_proposal(state, supabase)
    except Exception as e:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Apply Proposal Agent",
            subdir="validation",
            filename="apply_error.md",
            content=f"# Apply Proposal (Error)\n\n`{type(e).__name__}: {e}`\n",
        )
        state["status"] = f"❌ Apply Proposal error: {type(e).__name__}: {e}"
        return state
