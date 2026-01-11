# validation_bench_create_agent.py

from datetime import datetime
from typing import Dict, Any
from supabase import create_client
import os
import json

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def run_agent(
    workflow_id: str,
    user_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Creates a validation bench and maps instruments to it.
    Required in state:
      - bench_name
      - instrument_ids[]
    Optional:
      - bench_location
    """

    bench_name = state.get("bench_name")
    bench_location = state.get("bench_location", "")
    instrument_ids = state.get("instrument_ids", [])

    if not bench_name:
        state["status"] = "❌ bench_name is required"
        return state

    if not instrument_ids:
        state["status"] = "❌ No instruments selected for bench"
        return state

    # 1) Create bench
    bench_row = (
        supabase.table("validation_benches")
        .insert(
            {
                "name": bench_name,
                "location": bench_location,
                "status": "offline",
                "metadata": {
                    "created_by": user_id,
                    "created_via": "Validation_Create_Bench",
                },
            }
        )
        .execute()
        .data[0]
    )

    bench_id = bench_row["id"]

    # 2) Validate instruments belong to user
    instruments = (
        supabase.table("validation_instruments")
        .select("id")
        .in_("id", instrument_ids)
        .eq("user_id", user_id)
        .execute()
        .data
    )

    valid_ids = {i["id"] for i in instruments}

    for iid in instrument_ids:
        if iid not in valid_ids:
            state["status"] = f"❌ Instrument {iid} does not belong to user"
            return state

    # 3) Map instruments to bench
    mappings = []
    for iid in instrument_ids:
        mappings.append(
            {
                "bench_id": bench_id,
                "instrument_id": iid,
                "role": "unspecified",  # role can be refined later
            }
        )

    supabase.table("validation_bench_instruments").insert(mappings).execute()

    # 4) Artifacts
    report = {
        "bench_id": bench_id,
        "bench_name": bench_name,
        "location": bench_location,
        "instrument_count": len(mappings),
        "created_at": datetime.utcnow().isoformat(),
    }

    state["bench_id"] = bench_id
    state["bench_create_report"] = report

    return state
