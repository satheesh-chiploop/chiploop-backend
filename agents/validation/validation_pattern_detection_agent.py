# agents/validation/validation_pattern_detection_agent.py
import asyncio

def run_agent(state: dict) -> dict:
    """
    WF6: Pattern Detection (bench + plan cognition)
    Requires:
      - workflow_id
      - bench_id
      - test_plan_name
      - supabase_client injected by main.py
    """
    workflow_id = state.get("workflow_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")

    # ✅ MVP: use name, not id
    test_plan_name = (
        state.get("test_plan_name")
        or state.get("validation_test_plan_name")
        or state.get("test_plan")  # optional compatibility
    )

    supabase = state.get("supabase_client")

    if not workflow_id:
        state["status"] = "❌ Validation Pattern Detection: missing workflow_id"
        return state
    if not bench_id or not test_plan_name:
        state["status"] = "❌ Validation Pattern Detection: missing bench_id or test_plan_name"
        return state
    if supabase is None:
        state["status"] = "❌ Validation Pattern Detection: missing supabase_client in state"
        return state

    # ✅ Normalize into one key so downstream service has one contract
    state["test_plan_name"] = test_plan_name

    from services.validation.validation_pattern_detection_service import compute_and_store_validation_patterns

    try:
        asyncio.run(compute_and_store_validation_patterns(state, supabase))
    except RuntimeError as e:
        import nest_asyncio
        nest_asyncio.apply()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(compute_and_store_validation_patterns(state, supabase))

    return state



