import os, json, datetime
from utils.artifact_utils import save_text_artifact_and_record

# ---------------- Supabase client ----------------
try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

# Use the same env strategy as main.py
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def run_agent(state: dict) -> dict:
    """
    Resolves user's bench setup:
    - If instrument_ids provided: fetch those
    - Else: use user's default instruments from validation_instruments (is_default=true)
    Produces bench_setup.json used by downstream agents.
    """
    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    instrument_ids = state.get("instrument_ids")  # optional list[uuid]

    if not workflow_id or not user_id:
        state["status"] = "❌ Missing workflow_id or user_id"
        return state

    instruments = []
    if instrument_ids:
        instruments = (
            supabase.table("validation_instruments")
            .select("*")
            .in_("id", instrument_ids)
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
    else:
        instruments = (
            supabase.table("validation_instruments")
            .select("*")
            .eq("user_id", user_id)
            .eq("is_default", True)
            .execute()
            .data
            or []
        )

    bench_setup = {
        "workflow_id": workflow_id,
        "user_id": user_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "instruments": [
            {
                "id": inst["id"],
                "nickname": inst["nickname"],
                "vendor": inst.get("vendor"),
                "model": inst.get("model"),
                "instrument_type": inst["instrument_type"],  # psu|dmm|scope
                "transport": inst["transport"],              # pyvisa
                "interface": inst["interface"],              # USB|GPIB|TCPIP
                "resource_string": inst["resource_string"],
                "scpi_idn": inst.get("scpi_idn"),
                "capabilities": inst.get("capabilities") or {},
                "metadata": inst.get("metadata") or {},
                "is_default": inst.get("is_default", False),
            }
            for inst in instruments
        ],
    }

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        rel_path="validation/bench_setup.json",
        content=json.dumps(bench_setup, indent=2),
        content_type="application/json",
    )

    state["bench_setup"] = bench_setup
    state["status"] = "✅ Bench setup resolved"
    return state

