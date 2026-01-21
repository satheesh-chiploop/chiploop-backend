# backend/agents/validation/validation_apply_proposal_agent.py

def run_agent(state: dict) -> dict:
    """
    WF9: Apply Proposal Agent
    Deterministic DB mutation:
      - deactivates prior active plan(s) for user+name
      - inserts vNext plan as active
      - writes validation/apply_status.md
    """
    workflow_id = state.get("workflow_id")
    supabase = state.get("supabase_client")

    if not workflow_id:
        state["status"] = "❌ Validation Apply Proposal: missing workflow_id"
        return state
    if supabase is None:
        state["status"] = "❌ Validation Apply Proposal: missing supabase_client in state"
        return state

    from services.validation_apply_proposal_service import apply_test_plan_proposal

    try:
        return apply_test_plan_proposal(state, supabase)
    except Exception as e:
        # best-effort safe failure reporting
        state["status"] = f"❌ Apply Proposal error: {type(e).__name__}: {e}"
        return state
