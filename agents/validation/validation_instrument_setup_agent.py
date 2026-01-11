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
    print("\n[DEBUG][ValidationInstrumentSetup] ENTER run_agent")
    print("[DEBUG][ValidationInstrumentSetup] state keys:", list(state.keys()))

    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    instrument_ids = state.get("instrument_ids")  # optional list[uuid]
    bench_id = state.get("bench_id")  # optional uuid


        # ✅ Normalize instrument_ids: it may arrive as a JSON string like '["uuid"]'
    if isinstance(instrument_ids, str):
        try:
            instrument_ids = json.loads(instrument_ids)
        except Exception:
            # fallback: treat as single id string
            instrument_ids = [instrument_ids]

    if instrument_ids and not isinstance(instrument_ids, list):
        instrument_ids = [instrument_ids]


    print("[DEBUG][ValidationInstrumentSetup] workflow_id:", workflow_id)
    print("[DEBUG][ValidationInstrumentSetup] user_id:", user_id)
    print("[DEBUG][ValidationInstrumentSetup] instrument_ids:", instrument_ids)

    if not workflow_id or not user_id:
        print("[DEBUG][ValidationInstrumentSetup] ❌ Missing workflow_id or user_id")
        state["status"] = "❌ Missing workflow_id or user_id"
        return state

    print("[DEBUG][ValidationInstrumentSetup] Fetching instruments from Supabase...")

    instruments = []

    # 1) If bench_id is provided, resolve instruments from bench mappings (bench-driven)
    if bench_id:
        print("[DEBUG][ValidationInstrumentSetup] bench_id:", bench_id)
        mapping_rows = (
            supabase.table("validation_bench_instruments")
            .select("instrument_id,role")
            .eq("bench_id", bench_id)
            .execute()
            .data
            or []
        )

        mapped_ids = [r.get("instrument_id") for r in mapping_rows if r.get("instrument_id")]
        print("[DEBUG][ValidationInstrumentSetup] mapped instrument_ids:", mapped_ids)

        if mapped_ids:
            instruments = (
                supabase.table("validation_instruments")
                .select("*")
                .in_("id", mapped_ids)
                .eq("user_id", user_id)
                .execute()
                .data
                or []
            )

        # attach bench role onto each instrument (optional but useful downstream)
            role_by_id = {r.get("instrument_id"): r.get("role") for r in mapping_rows}
            for inst in instruments:
                inst["_bench_role"] = role_by_id.get(inst.get("id"))

        else:
            print("[DEBUG][ValidationInstrumentSetup] ⚠️ No rows in validation_bench_instruments for this bench_id")

    # 2) If explicit instrument_ids were provided (non-bench path), use them
    elif instrument_ids:
        instruments = (
            supabase.table("validation_instruments")
             .select("*")
             .in_("id", instrument_ids)
             .eq("user_id", user_id)
             .execute()
             .data
             or []
        )

    # 3) Fallback: default instruments (WF-1 / demo-friendly)
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
    
        print(
           "[DEBUG][ValidationInstrumentSetup] instruments fetched:",
           len(instruments),
        )

    bench_setup = {
        "workflow_id": workflow_id,
        "user_id": user_id,
        "bench_id": bench_id,
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

    
    print("[DEBUG][ValidationInstrumentSetup] bench_setup built")
    print("[DEBUG][ValidationInstrumentSetup] calling save_text_artifact_and_record()")

    # ✅ Upload artifact using the SAME convention as digital_spec_agent.py:
    # save_text_artifact_and_record(workflow_id, agent_name, subdir, filename, content)
    path = save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Instrument Setup Agent",
        subdir="validation",
        filename="bench_setup.json",
        content=json.dumps(bench_setup, indent=2),
    )

    print("[DEBUG][ValidationInstrumentSetup] artifact save returned:", path)

    state["bench_setup"] = bench_setup
    state["status"] = "✅ Bench setup resolved"
    return state

