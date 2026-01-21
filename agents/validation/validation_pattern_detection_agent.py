# agents/validation/validation_pattern_detection_agent.py

import asyncio

def run_agent(state: dict) -> dict:
    """
    WF5: Pattern Detection (bench-only cognition)
    Requires:
      - workflow_id
      - bench_id
      - test_plan_id
      - supabase_client injected by main.py
    """
    workflow_id = state.get("workflow_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")
    test_plan_id = state.get("test_plan_id") or state.get("validation_test_plan_id")
    supabase = state.get("supabase_client")

    if not workflow_id:
        state["status"] = "❌ Validation Pattern Detection: missing workflow_id"
        return state
    if not bench_id or not test_plan_id:
        state["status"] = "❌ Validation Pattern Detection: missing bench_id or test_plan_id"
        return state
    if supabase is None:
        state["status"] = "❌ Validation Pattern Detection: missing supabase_client in state"
        return state

    from services.validation.validation_pattern_detection_service import compute_and_store_validation_patterns

    try:
        asyncio.run(compute_and_store_validation_patterns(state, supabase))
    except RuntimeError as e:
        # Handles case where we're already inside an event loop (rare in your setup, but safe)
        import nest_asyncio
        nest_asyncio.apply()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(compute_and_store_validation_patterns(state, supabase))

    return state
