import os, json, datetime
from utils.artifact_utils import save_text_artifact_and_record

# ---------------- Supabase client ----------------
try:
    from supabase import create_client, Client  # supabase-py v2
except ImportError:
    raise RuntimeError("Please install supabase-py v2: pip install supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _now_iso():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def run_agent(state: dict) -> dict:
    """
    Validation Bench Schematic Load + Mapping Agent

    Purpose (WF4):
    - Load the bench schematic from validation_bench_connections (latest by updated_at)
    - Reconcile it against bench_setup (runtime instruments) to produce execution_mapping.json
    - Output demo-friendly artifacts:
        - validation/bench_schematic_loaded.json
        - validation/execution_mapping.json
        - validation/execution_mapping_summary.md

    Required state:
      - workflow_id
      - user_id
      - bench_id
      - bench_setup (from Validation Instrument Setup Agent)
    """

    print("\n[DEBUG][BenchSchematicLoadMapping] ENTER run_agent")
    print("[DEBUG][BenchSchematicLoadMapping] state keys:", list(state.keys()))

    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    bench_id = state.get("bench_id")
    bench_setup = state.get("bench_setup") or {}

    if not workflow_id or not user_id or not bench_id:
        state["status"] = "❌ Missing workflow_id, user_id, or bench_id"
        return state

    if not isinstance(bench_setup, dict) or not bench_setup.get("instruments"):
        state["status"] = "❌ Missing bench_setup; run 'Validation Instrument Setup Agent' before this agent."
        return state

    # 1) Load latest bench schematic from DB
    rows = (
        supabase.table("validation_bench_connections")
        .select("id,bench_id,schematic,updated_at")
        .eq("bench_id", bench_id)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )

    if not rows:
        state["status"] = "❌ No schematic found in validation_bench_connections for bench_id"
        # Still write an artifact for debugging
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Bench Schematic Load + Mapping Agent",
            subdir="validation",
            filename="bench_schematic_loaded.json",
            content=json.dumps({"bench_id": bench_id, "error": "not_found"}, indent=2),
        )
        return state

    row = rows[0]
    schematic = row.get("schematic") or {}
    updated_at = row.get("updated_at")

    # Save what we loaded (for transparency)
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Bench Schematic Load + Mapping Agent",
        subdir="validation",
        filename="bench_schematic_loaded.json",
        content=json.dumps(
            {
                "bench_id": bench_id,
                "updated_at": updated_at,
                "schematic": schematic,
            },
            indent=2,
        ),
    )

    # 2) Build index of runtime instruments from bench_setup
    # bench_setup instruments look like: { id, instrument_type, resource_string, nickname, ... }
    runtime_by_id = {}
    for inst in bench_setup.get("instruments", []) or []:
        iid = inst.get("id")
        if iid:
            runtime_by_id[str(iid)] = inst

    # Helper: enrich an instrument reference (instrument_id) -> include resource_string/nickname/type
    def enrich_instrument_ref(instrument_id: str):
        inst = runtime_by_id.get(str(instrument_id))
        if not inst:
            return None
        return {
            "instrument_id": inst.get("id"),
            "nickname": inst.get("nickname"),
            "instrument_type": inst.get("instrument_type"),
            "resource_string": inst.get("resource_string"),
        }

    issues = []

    # 3) Create execution mapping from schematic.connections (MVP)
    connections = (schematic or {}).get("connections") or {}
    rails = connections.get("rails") or []
    probes = connections.get("probes") or []

    mapped_rails = []
    for r in rails:
        rail_name = r.get("rail_name")
        psu_ref = (r.get("psu") or {}).get("instrument_id")
        psu_ch = (r.get("psu") or {}).get("channel")
        sense_ref = (r.get("sense") or {}).get("dmm_instrument_id")
        sense_mode = (r.get("sense") or {}).get("mode")

        psu_enriched = enrich_instrument_ref(psu_ref) if psu_ref else None
        sense_enriched = enrich_instrument_ref(sense_ref) if sense_ref else None

        if psu_ref and not psu_enriched:
            issues.append({"type": "missing_runtime_instrument", "where": "rail.psu", "rail": rail_name, "instrument_id": psu_ref})
        if sense_ref and not sense_enriched:
            issues.append({"type": "missing_runtime_instrument", "where": "rail.sense", "rail": rail_name, "instrument_id": sense_ref})

        mapped_rails.append(
            {
                "rail_name": rail_name,
                "dut_points": r.get("dut_points") or [],
                "psu": {
                    **(psu_enriched or {"instrument_id": psu_ref}),
                    "channel": psu_ch,
                },
                "sense": {
                    **(sense_enriched or {"instrument_id": sense_ref}),
                    "mode": sense_mode,
                },
            }
        )

    mapped_probes = []
    for p in probes:
        sig = p.get("signal_name")
        scope_ref = (p.get("scope") or {}).get("instrument_id")
        scope_ch = (p.get("scope") or {}).get("channel")

        scope_enriched = enrich_instrument_ref(scope_ref) if scope_ref else None

        if scope_ref and not scope_enriched:
            issues.append({"type": "missing_runtime_instrument", "where": "probe.scope", "signal": sig, "instrument_id": scope_ref})

        mapped_probes.append(
            {
                "signal_name": sig,
                "dut_points": p.get("dut_points") or [],
                "probe_type": p.get("probe_type"),
                "scope": {
                    **(scope_enriched or {"instrument_id": scope_ref}),
                    "channel": scope_ch,
                },
            }
        )

    # executor/mode: mirror whatever WF4 is using (default stub)
    executor = state.get("executor") or state.get("mode") or "stub"

    execution_mapping = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "bench_id": bench_id,
        "bench_connections_row_id": row.get("id"),
        "schematic_updated_at": updated_at,
        "executor": executor,
        "mappings": {
            "rails": mapped_rails,
            "probes": mapped_probes,
        },
        "issues": issues,
    }

    # 4) Save mapping artifact
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Bench Schematic Load + Mapping Agent",
        subdir="validation",
        filename="execution_mapping.json",
        content=json.dumps(execution_mapping, indent=2),
    )

    # 5) Summary MD (demo-friendly)
    lines = []
    lines.append("# Execution Mapping (MVP)")
    lines.append("")
    lines.append(f"- Bench ID: `{bench_id}`")
    lines.append(f"- Schematic updated_at: `{updated_at}`")
    lines.append(f"- Executor: **{executor}**")
    lines.append("")
    lines.append("## Rails")
    if mapped_rails:
        for r in mapped_rails:
            psu = r.get("psu") or {}
            sense = r.get("sense") or {}
            lines.append(
                f"- **{r.get('rail_name')}**: PSU({psu.get('nickname') or psu.get('instrument_id')}) CH{psu.get('channel')} "
                f"→ DUT {r.get('dut_points')} | Sense({sense.get('nickname') or sense.get('instrument_id')}) {sense.get('mode')}"
            )
    else:
        lines.append("- (none)")

    lines.append("")
    lines.append("## Probes")
    if mapped_probes:
        for p in mapped_probes:
            sc = p.get("scope") or {}
            lines.append(
                f"- **{p.get('signal_name')}**: Scope({sc.get('nickname') or sc.get('instrument_id')}) CH{sc.get('channel')} "
                f"→ DUT {p.get('dut_points')}"
            )
    else:
        lines.append("- (none)")

    lines.append("")
    if issues:
        lines.append("## Issues")
        for i in issues:
            lines.append(f"- {json.dumps(i)}")
    else:
        lines.append("## Issues")
        lines.append("- (none)")

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Bench Schematic Load + Mapping Agent",
        subdir="validation",
        filename="execution_mapping_summary.md",
        content="\n".join(lines),
    )

    # 6) Update state for downstream agents
    state["bench_schematic"] = schematic
    state["execution_mapping"] = execution_mapping
    state["status"] = "✅ Bench schematic loaded and execution mapping generated"
    return state
