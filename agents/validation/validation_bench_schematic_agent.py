# agents/validation/validation_bench_schematic_agent.py
#
# Validation Bench Schematic Agent
# - Generates bench_schematic.json (MVP) from bench + mapped instruments
# - Stores it in validation_bench_connections.schematic
# - Writes artifacts:
#     validation/bench_schematic.json
#     validation/bench_schematic_summary.md
#
# Expected state inputs:
#   - workflow_id: str (required)
#   - user_id: str (required)
#   - bench_id: str (required)
#
import os
import json
import datetime
import logging
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

logger = logging.getLogger(__name__)

try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _now_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _norm(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _pick_first_by_type(instruments: List[Dict[str, Any]], inst_type: str) -> Optional[Dict[str, Any]]:
    t = inst_type.strip().lower()
    for inst in instruments or []:
        if _norm(inst.get("instrument_type")).lower() == t:
            return inst
    return None


def _load_bench(bench_id: str) -> Optional[Dict[str, Any]]:
    rows = (
        supabase.table("validation_benches")
        .select("id,name,location,status,metadata,created_at")
        .eq("id", bench_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _load_bench_instrument_mappings(bench_id: str) -> List[Dict[str, Any]]:
    return (
        supabase.table("validation_bench_instruments")
        .select("instrument_id,role,created_at")
        .eq("bench_id", bench_id)
        .execute()
        .data
        or []
    )


def _load_instruments(user_id: str, instrument_ids: List[str]) -> List[Dict[str, Any]]:
    if not instrument_ids:
        return []
    return (
        supabase.table("validation_instruments")
        .select("id,nickname,vendor,model,instrument_type,transport,interface,resource_string,capabilities,metadata,user_id")
        .in_("id", instrument_ids)
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )


def _upsert_bench_connections(bench_id: str, schematic: Dict[str, Any]) -> None:
    # Your table has:
    #   id (pk), bench_id (fk), schematic (jsonb), updated_at
    # And bench_id is NOT unique, so we enforce 1-row-per-bench in code:
    existing = (
        supabase.table("validation_bench_connections")
        .select("id")
        .eq("bench_id", bench_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )

    if existing:
        row_id = existing[0]["id"]
        supabase.table("validation_bench_connections").update(
            {
                "schematic": schematic,
                "updated_at": _now_iso(),
            }
        ).eq("id", row_id).execute()
    else:
        supabase.table("validation_bench_connections").insert(
            {
                "bench_id": bench_id,
                "schematic": schematic,
                "updated_at": _now_iso(),
            }
        ).execute()


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    bench_id = state.get("bench_id")

    if not workflow_id or not user_id or not bench_id:
        state["status"] = "❌ Missing workflow_id, user_id, or bench_id"
        return state

    bench = _load_bench(bench_id)
    if not bench:
        state["status"] = f"❌ Bench not found: {bench_id}"
        return state

    mappings = _load_bench_instrument_mappings(bench_id)
    mapped_ids = [m.get("instrument_id") for m in mappings if m.get("instrument_id")]
    if not mapped_ids:
        state["status"] = "❌ No instruments mapped to this bench (validation_bench_instruments empty)"
        return state

    inst_rows = _load_instruments(user_id, mapped_ids)

    role_by_id = {m.get("instrument_id"): m.get("role") for m in mappings}

    instruments: List[Dict[str, Any]] = []
    for r in inst_rows:
        instruments.append(
            {
                "instrument_id": r.get("id"),
                "nickname": r.get("nickname"),
                "vendor": r.get("vendor"),
                "model": r.get("model"),
                "instrument_type": r.get("instrument_type"),
                "transport": r.get("transport"),
                "interface": r.get("interface"),
                "resource_string": r.get("resource_string"),
                "role": role_by_id.get(r.get("id")) or r.get("instrument_type") or "unspecified",
                "capabilities": r.get("capabilities") or {},
                "metadata": r.get("metadata") or {},
            }
        )

    # MVP connections template: create a minimal schematic the UI can visualize later.
    psu = _pick_first_by_type(instruments, "psu")
    dmm = _pick_first_by_type(instruments, "dmm")
    scope = _pick_first_by_type(instruments, "scope")

    rails: List[Dict[str, Any]] = []
    probes: List[Dict[str, Any]] = []

    if psu:
        rails.append(
            {
                "rail_name": "VIN",
                "psu": {"instrument_id": psu["instrument_id"], "channel": 1},
                "sense": {"dmm_instrument_id": (dmm["instrument_id"] if dmm else None), "mode": "vdc"},
                "dut_points": ["VIN", "GND"],
            }
        )

    if scope:
        probes.append(
            {
                "signal_name": "VOUT",
                "scope": {"instrument_id": scope["instrument_id"], "channel": 1},
                "dut_points": ["VOUT", "GND"],
                "probe_type": "passive",
            }
        )

    schematic: Dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "bench": {
            "bench_id": bench_id,
            "name": bench.get("name"),
            "location": bench.get("location"),
        },
        "dut": {
            "name": state.get("dut_name") or "DUT",
            "notes": state.get("dut_notes") or "",
        },
        "instruments": instruments,
        "connections": {
            "rails": rails,
            "probes": probes,
        },
        "notes": [],
    }

    # Persist to DB (validation_bench_connections)
    _upsert_bench_connections(bench_id, schematic)

    # Artifacts
    agent_name = "Validation Bench Schematic Agent"
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="bench_schematic.json",
        content=json.dumps(schematic, indent=2),
    )

    summary_md = "\n".join(
        [
            "# Bench Schematic (MVP)",
            "",
            f"- Bench: **{bench.get('name')}**",
            f"- Bench ID: `{bench_id}`",
            f"- Instruments mapped: **{len(instruments)}**",
            f"- Rails templates: **{len(rails)}**",
            f"- Probe templates: **{len(probes)}**",
            "",
            "This is an MVP schematic meant to be refined. Next step: WF3 Preflight should verify schematic presence + basic integrity.",
        ]
    )

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="bench_schematic_summary.md",
        content=summary_md,
    )

    state["bench_schematic"] = schematic
    state["status"] = "✅ Bench schematic generated and stored"
    return state
