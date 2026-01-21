# agents/validation/validation_evolution_proposal_agent.py
import asyncio

def run_agent(state: dict) -> dict:
    supabase = state.get("supabase_client")
    if supabase is None:
        state["status"] = "❌ WF7 missing supabase_client"
        return state

    from services.validation.validation_evolution_proposal_service import compute_and_store_evolution_proposal

    try:
        asyncio.run(compute_and_store_evolution_proposal(state, supabase))
    except RuntimeError:
        # already in event loop
        import nest_asyncio
        nest_asyncio.apply()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(compute_and_store_evolution_proposal(state, supabase))

    return state
