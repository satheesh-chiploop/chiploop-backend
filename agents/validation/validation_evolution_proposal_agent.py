# agents/validation/validation_evolution_proposal_agent.py
import asyncio


def run_agent(state: dict) -> dict:
    """
    WF7: Evolution Proposal (failure-driven)
    Requires:
      - workflow_id
      - bench_id
      - test_plan_name   (MVP: name, not id)
      - supabase_client injected by main.py
    """

    # ✅ Normalize bench_id
    bench_id = state.get("bench_id") or state.get("validation_bench_id")

    # ✅ MVP: Normalize test_plan_name (name-based)
    test_plan_name = (
        state.get("test_plan_name")
        or state.get("validation_test_plan_name")
        or state.get("test_plan")  # optional compatibility
    )

    # ✅ Write normalized keys back so the service has one contract
    if bench_id:
        state["bench_id"] = bench_id
    if test_plan_name:
        state["test_plan_name"] = str(test_plan_name).strip()

    supabase = state.get("supabase_client")
    if supabase is None:
        state["status"] = "❌ WF7 missing supabase_client"
        return state

    from services.validation.validation_evolution_proposal_service import (
        compute_and_store_evolution_proposal,
    )

    try:
        asyncio.run(compute_and_store_evolution_proposal(state, supabase))
    except RuntimeError:
        # already in event loop
        import nest_asyncio

        nest_asyncio.apply()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(compute_and_store_evolution_proposal(state, supabase))

    return state

