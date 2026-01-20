# services/validation_memory_service.py
#
# ChipLoop Cognition: Validation Memory
#
# - Loads WF4 artifacts (prefer from state; fallback to Supabase storage via workflows.artifacts)
# - Idempotency via validation_memory_ingestion_log (run_id + artifact_hash)
# - Writes:
#     validation_run_facts
#     validation_run_interpretations (LLM output per failing fact)
#     validation_memory (aggregated, evidence-backed)
# - Writes workflow artifacts:
#     validation/memory_events.json
#     validation/memory_summary.md

from __future__ import annotations

import os
import json
import hashlib
import datetime
import asyncio
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record
from utils.llm_utils import run_llm_fallback

ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")
PROMPT_VERSION = "v1"


# -----------------------------
# Small utilities
# -----------------------------
def _utcnow_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _iter_leaf_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_leaf_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_leaf_strings(v)
    elif isinstance(obj, str):
        s = obj.strip()
        if s:
            yield s


def _normalize_storage_path(p: str) -> str:
    p = (p or "").strip()
    if p.startswith("/artifacts/anonymous/"):
        return p[len("/artifacts/anonymous/") :]
    if p.startswith("/artifacts/"):
        return p[len("/artifacts/") :]
    if p.startswith("/"):
        return p[1:]
    return p


def _safe_json_load(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(x, str):
        try:
            obj = json.loads(x)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _stable_hash_payload(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8", errors="ignore")
    return hashlib.sha256(blob).hexdigest()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


async def _call_llm(prompt: str) -> str:
    """
    run_llm_fallback is used both sync and async in your codebase.
    This wrapper makes it safe.
    """
    try:
        out = run_llm_fallback(prompt)
        if asyncio.iscoroutine(out):
            out = await out
        return str(out)
    except TypeError:
        # Some versions only accept messages=...
        out = run_llm_fallback(messages=[{"role": "user", "content": prompt}])
        if asyncio.iscoroutine(out):
            out = await out
        return str(out)


def _find_storage_key_for_filename(artifacts: Dict[str, Any], filename: str) -> Optional[str]:
    if not isinstance(artifacts, dict):
        return None

    candidates: List[str] = []
    for raw in _iter_leaf_strings(artifacts):
        sp = _normalize_storage_path(raw)
        if sp.endswith("/" + filename) or sp.endswith(filename):
            candidates.append(sp)

    # Prefer backend/... first (your canonical keys)
    for c in candidates:
        if c.startswith("backend/"):
            return c
    return candidates[0] if candidates else None


def _download_json_from_storage(supabase, storage_key: str) -> Dict[str, Any]:
    if not storage_key:
        return {}
    try:
        blob = supabase.storage.from_(ARTIFACT_BUCKET).download(storage_key)
        return _safe_json_load(blob)
    except Exception:
        return {}


# -----------------------------
# Fact extraction
# -----------------------------
def _compute_failure_signature(
    test_name: str,
    metric: Optional[str],
    direction: Optional[str],
    instrument_hints: List[str],
    dut_hints: List[str],
) -> str:
    base = {
        "test_name": (test_name or "").strip(),
        "metric": (metric or "").strip(),
        "direction": (direction or "").strip(),
        "instrument_hints": instrument_hints or [],
        "dut_hints": dut_hints or [],
    }
    return _sha1(json.dumps(base, sort_keys=True))


def _extract_fact_rows(
    run_id: str,
    bench_id: Optional[str],
    test_plan_id: Optional[str],
    results: Dict[str, Any],
    analytics: Dict[str, Any],
    artifacts_ref: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Creates BOTH:
      A) per-test facts (metric="__test__")
      B) per-check facts (metric = signal)
    using analytics as the source of truth for pass/fail + limits.
    """
    fact_rows: List[Dict[str, Any]] = []
    created_at = _utcnow_iso()

    # Index results by test name (in case we want raw step errors)
    results_by_test: Dict[str, Dict[str, Any]] = {}
    for t in (results.get("tests") or []):
        if isinstance(t, dict):
            name = t.get("name") or t.get("test_name") or "unnamed_test"
            results_by_test[str(name)] = t

    dut = (analytics.get("dut") or {}) if isinstance(analytics, dict) else {}

    for t in (analytics.get("tests") or []):
        if not isinstance(t, dict):
            continue

        test_name = t.get("name") or "unnamed_test"
        test_pass = bool(t.get("pass"))

        # Test-level fact
        test_status = "pass" if test_pass else "fail"
        test_sig = _compute_failure_signature(
            test_name=test_name,
            metric="__test__",
            direction="",
            instrument_hints=[*map(str, (t.get("required_instruments") or []))],
            dut_hints=[str(dut.get("name") or ""), str(dut.get("part") or "")],
        )
        fact_rows.append({
            "run_id": run_id,
            "bench_id": bench_id,
            "test_plan_id": test_plan_id,
            "test_name": test_name,
            "status": test_status,
            "failure_signature": test_sig if test_status != "pass" else None,
            "failure_type_raw": "test_result",
            "metric": "__test__",
            "value": None,
            "limit_low": None,
            "limit_high": None,
            "units": None,
            "instrument_refs": {"required_instruments": t.get("required_instruments") or []},
            "dut_refs": dut if isinstance(dut, dict) else {},
            "conditions": {"mode": analytics.get("mode")},
            "artifact_refs": artifacts_ref,
            "created_at": created_at,
        })

        # Check-level facts (signals)
        test_result = results_by_test.get(str(test_name)) or {}
        steps = test_result.get("steps") if isinstance(test_result, dict) else None

        for c in (t.get("checks") or []):
            if not isinstance(c, dict):
                continue

            signal = c.get("signal") or "unnamed_signal"
            status = c.get("status") or "missing"  # pass/fail/missing
            measured = _safe_float(c.get("measured"))
            lim = c.get("limit") or {}
            mn = _safe_float(lim.get("min"))
            mx = _safe_float(lim.get("max"))
            units = c.get("units")

            mapped_status = "pass" if status == "pass" else ("fail" if status == "fail" else "error")

            direction = ""
            if measured is not None:
                if mx is not None and measured > mx:
                    direction = "above_high"
                elif mn is not None and measured < mn:
                    direction = "below_low"

            # instrument hints: required instruments + any captured idn keys
            instrument_hints = []
            req = t.get("required_instruments") or []
            if isinstance(req, list):
                instrument_hints.extend([str(x) for x in req])

            # if results captures have *_idn, treat that as hint
            caps = test_result.get("captures") if isinstance(test_result, dict) else {}
            if isinstance(caps, dict):
                for k in caps.keys():
                    if str(k).lower().endswith("_idn"):
                        instrument_hints.append(str(k))

            dut_hints = [str(dut.get("name") or ""), str(dut.get("part") or ""), str(dut.get("rail") or "")]

            sig = None
            if mapped_status in ("fail", "error"):
                sig = _compute_failure_signature(
                    test_name=test_name,
                    metric=str(signal),
                    direction=direction,
                    instrument_hints=instrument_hints,
                    dut_hints=dut_hints,
                )

            # raw failure type from steps if we have any scpi error
            failure_type_raw = None
            if isinstance(steps, list):
                for st in steps:
                    if isinstance(st, dict) and st.get("ok") is False:
                        failure_type_raw = "scpi_error"
                        break

            fact_rows.append({
                "run_id": run_id,
                "bench_id": bench_id,
                "test_plan_id": test_plan_id,
                "test_name": test_name,
                "status": mapped_status,
                "failure_signature": sig,
                "failure_type_raw": failure_type_raw,
                "metric": str(signal),
                "value": measured,
                "limit_low": mn,
                "limit_high": mx,
                "units": units,
                "instrument_refs": {"required_instruments": req, "captures_keys": list(caps.keys()) if isinstance(caps, dict) else []},
                "dut_refs": dut if isinstance(dut, dict) else {},
                "conditions": {"mode": analytics.get("mode")},
                "artifact_refs": artifacts_ref,
                "created_at": created_at,
            })

    return fact_rows


# -----------------------------
# LLM interpretation
# -----------------------------
def _build_interpret_prompt(fact: Dict[str, Any], context: Dict[str, Any]) -> str:
    return f"""
You are ChipLoop's Validation Failure Interpreter.

CONTEXT (trusted):
{json.dumps(context, indent=2)}

FACT (from run_facts):
{json.dumps(fact, indent=2)}

Task:
- Classify the most likely failure_category (free-text label, snake_case preferred).
- Provide short summary and explanation grounded in the FACT.
- Provide confidence in [0,1].
- Provide signals_used and recommendations.

Return STRICT JSON ONLY:
{{
  "failure_category": "string",
  "confidence": 0.0,
  "summary": "string",
  "explanation": "string",
  "signals_used": ["string"],
  "recommendations": ["string"]
}}
"""


def _validate_interp(o: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(o, dict):
        return None
    fc = o.get("failure_category")
    conf = o.get("confidence")
    summ = o.get("summary")
    expl = o.get("explanation")
    if not isinstance(fc, str) or not fc.strip():
        return None
    try:
        conf_f = float(conf)
    except Exception:
        return None
    if conf_f < 0 or conf_f > 1:
        return None
    if not isinstance(summ, str) or not summ.strip():
        return None
    if not isinstance(expl, str) or not expl.strip():
        return None
    signals = o.get("signals_used") if isinstance(o.get("signals_used"), list) else []
    recs = o.get("recommendations") if isinstance(o.get("recommendations"), list) else []
    return {
        "failure_category": fc.strip(),
        "confidence": conf_f,
        "summary": summ.strip(),
        "explanation": expl.strip(),
        "signals_used": signals,
        "recommendations": recs,
    }


# -----------------------------
# Memory aggregation (writes validation_memory)
# -----------------------------
MEM_RECURRING = "recurring_failure"
MEM_FLAKY = "flaky_test"
MEM_BENCH = "bench_quirk"


def _dedupe_runs(existing: List[str], new_run: str, max_n: int = 15) -> List[str]:
    out = [x for x in existing if isinstance(x, str)]
    if new_run and new_run not in out:
        out.append(new_run)
    return out[-max_n:]


def _weighted_conf(old_conf: float, old_n: int, new_conf: float) -> float:
    if old_n <= 0:
        return float(new_conf)
    return (old_conf * old_n + float(new_conf)) / float(old_n + 1)


def _promote_recurring_memory(
    supabase,
    bench_id: str,
    test_plan_id: str,
    test_name: str,
    failure_signature: str,
    run_id: str,
    now_iso: str,
) -> int:
    # Gather facts history for this signature
    hist = (
        supabase.table("validation_run_facts")
        .select("id,status,created_at,run_id")
        .eq("bench_id", bench_id)
        .eq("test_plan_id", test_plan_id)
        .eq("test_name", test_name)
        .eq("failure_signature", failure_signature)
        .execute()
    )
    rows = hist.data or []
    if len(rows) < 2:
        return 0

    freq = len(rows)
    fail_count = sum(1 for r in rows if r.get("status") in ("fail", "error"))
    pass_count = sum(1 for r in rows if r.get("status") == "pass")

    created_times = [r.get("created_at") for r in rows if r.get("created_at")]
    first_seen = min(created_times) if created_times else now_iso
    last_seen = max(created_times) if created_times else now_iso

    # Dominant category from interpretations
    fact_ids = [r.get("id") for r in rows if r.get("id")]
    failure_category = None
    avg_conf = 0.0
    if fact_ids:
        ints = (
            supabase.table("validation_run_interpretations")
            .select("failure_category,confidence,fact_id")
            .in_("fact_id", fact_ids)
            .execute()
        )
        idata = ints.data or []
        by_cat: Dict[str, Tuple[float, int]] = {}
        for it in idata:
            cat = it.get("failure_category")
            conf = it.get("confidence")
            try:
                conf = float(conf)
            except Exception:
                conf = 0.0
            if isinstance(cat, str) and cat.strip():
                s, n = by_cat.get(cat, (0.0, 0))
                by_cat[cat] = (s + conf, n + 1)
        if by_cat:
            failure_category = sorted(by_cat.items(), key=lambda kv: kv[1][0], reverse=True)[0][0]
            total_conf = sum(v[0] for v in by_cat.values())
            total_n = sum(v[1] for v in by_cat.values())
            avg_conf = (total_conf / total_n) if total_n else 0.0

    existing = (
        supabase.table("validation_memory")
        .select("id,frequency,confidence,evidence_run_ids,fail_count,pass_count")
        .eq("bench_id", bench_id)
        .eq("test_plan_id", test_plan_id)
        .eq("test_name", test_name)
        .eq("memory_kind", MEM_RECURRING)
        .eq("failure_signature", failure_signature)
        .limit(1)
        .execute()
    )
    data = existing.data or []
    summary = f"Recurring failure signature seen {freq}× for '{test_name}' ({failure_signature[:8]}…)."

    if data:
        row0 = data[0]
        old_freq = int(row0.get("frequency") or 0)
        old_conf = float(row0.get("confidence") or 0.0)
        ev = row0.get("evidence_run_ids") or []
        new_ev = _dedupe_runs(ev if isinstance(ev, list) else [], run_id)
        new_conf = _weighted_conf(old_conf, old_freq, avg_conf if avg_conf else old_conf)

        supabase.table("validation_memory").update({
            "frequency": freq,
            "fail_count": fail_count,
            "pass_count": pass_count,
            "last_seen": last_seen,
            "confidence": new_conf,
            "failure_category": failure_category,
            "severity": "high" if fail_count >= 3 else "medium",
            "summary": summary,
            "metadata": {
                "dominant_category": failure_category,
                "avg_confidence": avg_conf,
                "note": "auto-aggregated",
            },
            "evidence_run_ids": new_ev,
        }).eq("id", row0["id"]).execute()
    else:
        supabase.table("validation_memory").insert({
            "bench_id": bench_id,
            "test_plan_id": test_plan_id,
            "test_name": test_name,
            "memory_kind": MEM_RECURRING,
            "failure_signature": failure_signature,
            "failure_category": failure_category,
            "frequency": freq,
            "fail_count": fail_count,
            "pass_count": pass_count,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "confidence": avg_conf,
            "severity": "high" if fail_count >= 3 else "medium",
            "summary": summary,
            "metadata": {
                "dominant_category": failure_category,
                "avg_confidence": avg_conf,
                "note": "auto-aggregated",
            },
            "evidence_run_ids": [run_id] if run_id else [],
        }).execute()

    return 1


def _promote_flaky_memory(
    supabase,
    bench_id: str,
    test_plan_id: str,
    test_name: str,
    run_id: str,
    now_iso: str,
    window: int = 6,
) -> int:
    q = (
        supabase.table("validation_run_facts")
        .select("run_id,status,created_at")
        .eq("bench_id", bench_id)
        .eq("test_plan_id", test_plan_id)
        .eq("test_name", test_name)
        .eq("metric", "__test__")
        .order("created_at", desc=True)
        .limit(window)
        .execute()
    )
    rows = q.data or []
    if len(rows) < window:
        return 0

    fails = sum(1 for r in rows if r.get("status") in ("fail", "error"))
    passes = sum(1 for r in rows if r.get("status") == "pass")
    total = fails + passes
    if total < window:
        return 0

    fail_rate = fails / total
    if not (0.2 <= fail_rate <= 0.8 and fails >= 2 and passes >= 2):
        return 0

    first_seen = rows[-1].get("created_at") or now_iso
    last_seen = rows[0].get("created_at") or now_iso
    evidence = [r.get("run_id") for r in rows if r.get("run_id")]
    summary = f"Flaky test '{test_name}': {fails} fails / {passes} passes in last {total} runs."

    existing = (
        supabase.table("validation_memory")
        .select("id,evidence_run_ids,frequency")
        .eq("bench_id", bench_id)
        .eq("test_plan_id", test_plan_id)
        .eq("test_name", test_name)
        .eq("memory_kind", MEM_FLAKY)
        .limit(1)
        .execute()
    )
    data = existing.data or []
    if data:
        row0 = data[0]
        ev = row0.get("evidence_run_ids") or []
        new_ev = _dedupe_runs(ev if isinstance(ev, list) else [], run_id)
        supabase.table("validation_memory").update({
            "frequency": len(rows),
            "fail_count": fails,
            "pass_count": passes,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "confidence": 0.7,
            "severity": "medium",
            "summary": summary,
            "metadata": {"fail_rate": fail_rate, "window": window},
            "evidence_run_ids": new_ev,
        }).eq("id", row0["id"]).execute()
    else:
        supabase.table("validation_memory").insert({
            "bench_id": bench_id,
            "test_plan_id": test_plan_id,
            "test_name": test_name,
            "memory_kind": MEM_FLAKY,
            "failure_signature": None,
            "failure_category": None,
            "frequency": len(rows),
            "fail_count": fails,
            "pass_count": passes,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "confidence": 0.7,
            "severity": "medium",
            "summary": summary,
            "metadata": {"fail_rate": fail_rate, "window": window},
            "evidence_run_ids": evidence[-15:],
        }).execute()

    return 1


# -----------------------------
# Main entry point
# -----------------------------
async def compute_and_store_validation_memory(
    state: Dict[str, Any],
    supabase,  # pass your existing supabase client from main.py
) -> Dict[str, Any]:
    """
    Call this after Validation Analytics Agent (WF4).
    Expects state has:
      - workflow_id
      - run_id (recommended)
      - bench_id and test_plan_id (required for memory promotion)
      - validation_results + validation_analytics (preferred)
    """
    workflow_id = state.get("workflow_id")
    run_id = state.get("run_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")
    test_plan_id = state.get("test_plan_id") or state.get("validation_test_plan_id")

    if not workflow_id:
        state["status"] = "❌ Validation Memory: missing workflow_id"
        return state

    if not bench_id or not test_plan_id:
        state["status"] = "❌ Validation Memory: missing bench_id or test_plan_id"
        return state

    # Load workflows.artifacts for fallback fetch
    wf_row = (
        supabase.table("workflows")
        .select("artifacts")
        .eq("id", workflow_id)
        .single()
        .execute()
    )
    artifacts_tree = (wf_row.data or {}).get("artifacts") or {}
    if not isinstance(artifacts_tree, dict):
        artifacts_tree = {}

    # Prefer state artifacts (fast + deterministic)
    results = state.get("validation_results")
    analytics = state.get("validation_analytics")

    # Fallback to storage if missing
    if not isinstance(results, dict) or not results:
        key = _find_storage_key_for_filename(artifacts_tree, "results.json")
        results = _download_json_from_storage(supabase, key) if key else {}

    if not isinstance(analytics, dict) or not analytics:
        key = _find_storage_key_for_filename(artifacts_tree, "analytics.json")
        analytics = _download_json_from_storage(supabase, key) if key else {}

    if not isinstance(results, dict) or not results:
        state["status"] = "❌ Validation Memory: could not load results.json"
        return state

    if not isinstance(analytics, dict) or not analytics:
        state["status"] = "❌ Validation Memory: could not load analytics.json"
        return state

    # Build artifact refs (useful for traceability)
    artifacts_ref = {
        "results_json": "validation/results.json",
        "analytics_json": "validation/analytics.json",
        "run_manifest": "validation/run_manifest.json",
    }

    # Idempotency: hash results + analytics
    artifact_hash = _stable_hash_payload(results, analytics)

    # If already ingested for same hash -> no-op
    existing = (
        supabase.table("validation_memory_ingestion_log")
        .select("run_id,artifact_hash")
        .eq("run_id", run_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        old = existing.data[0]
        if old.get("artifact_hash") == artifact_hash:
            state["status"] = "✅ Validation Memory: no-op (already ingested for this run)"
            return state

    # 1) Extract facts
    fact_rows = _extract_fact_rows(
        run_id=run_id,
        bench_id=str(bench_id),
        test_plan_id=str(test_plan_id),
        results=results,
        analytics=analytics,
        artifacts_ref=artifacts_ref,
    )

    inserted_facts = []
    if fact_rows:
        resp = supabase.table("validation_run_facts").insert(fact_rows).execute()
        inserted_facts = resp.data or []

    # 2) Interpret failing facts via LLM
    model_name = os.getenv("CHIPLOOP_LLM_MODEL", "auto")
    context = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "bench_id": str(bench_id),
        "test_plan_id": str(test_plan_id),
        "analytics_mode": analytics.get("mode"),
    }

    interp_payloads: List[Dict[str, Any]] = []
    for f in inserted_facts:
        if f.get("status") not in ("fail", "error"):
            continue

        prompt = _build_interpret_prompt(f, context)
        llm_raw = await _call_llm(prompt)
        parsed = _safe_json_load(llm_raw)
        clean = _validate_interp(parsed)
        if not clean:
            clean = {
                "failure_category": "unknown",
                "confidence": 0.2,
                "summary": "Unable to reliably classify failure from available evidence.",
                "explanation": "LLM output was invalid or incomplete. Treat as unknown and inspect raw results/analytics.",
                "signals_used": [],
                "recommendations": ["Inspect analytics.json and results.json for this test and signal."],
            }

        interp_payloads.append({
            "fact_id": f["id"],
            "run_id": run_id,
            "model": model_name,
            "prompt_version": PROMPT_VERSION,
            "failure_category": clean["failure_category"],
            "confidence": clean["confidence"],
            "summary": clean["summary"],
            "explanation": clean["explanation"],
            "signals_used": clean["signals_used"],
            "recommendations": clean["recommendations"],
        })

    if interp_payloads:
        supabase.table("validation_run_interpretations").insert(interp_payloads).execute()

    # 3) Promote memory (recurring + flaky)
    now_iso = _utcnow_iso()
    promotions = 0

    for f in inserted_facts:
        sig = f.get("failure_signature")
        if not sig:
            continue
        if f.get("status") not in ("fail", "error"):
            continue

        promotions += _promote_recurring_memory(
            supabase=supabase,
            bench_id=str(bench_id),
            test_plan_id=str(test_plan_id),
            test_name=str(f.get("test_name") or "unnamed_test"),
            failure_signature=str(sig),
            run_id=run_id,
            now_iso=now_iso,
        )

    test_names = [t.get("name") for t in (analytics.get("tests") or []) if isinstance(t, dict)]
    for tn in sorted({str(x) for x in test_names if x}):
        promotions += _promote_flaky_memory(
            supabase=supabase,
            bench_id=str(bench_id),
            test_plan_id=str(test_plan_id),
            test_name=tn,
            run_id=run_id,
            now_iso=now_iso,
            window=6,
        )

    # 4) Write ingestion log
    if run_id:
        supabase.table("validation_memory_ingestion_log").upsert({
            "run_id": run_id,
            "artifact_hash": artifact_hash,
        }).execute()

    # 5) Write workflow artifacts for UI visibility
    events = {
        "run_id": run_id,
        "bench_id": str(bench_id),
        "test_plan_id": str(test_plan_id),
        "facts_inserted": len(inserted_facts),
        "interpretations_inserted": len(interp_payloads),
        "memory_promotions": promotions,
        "artifact_hash": artifact_hash,
        "timestamp": now_iso,
    }

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Memory Service",
        subdir="validation",
        filename="memory_events.json",
        content=json.dumps(events, indent=2),
    )

    summary_lines = []
    summary_lines.append("# Validation Memory Ingestion\n")
    summary_lines.append(f"- Run: `{run_id}`")
    summary_lines.append(f"- Bench: `{bench_id}`")
    summary_lines.append(f"- Test plan: `{test_plan_id}`")
    summary_lines.append(f"- Facts inserted: **{len(inserted_facts)}**")
    summary_lines.append(f"- Interpretations inserted: **{len(interp_payloads)}**")
    summary_lines.append(f"- Memory promotions: **{promotions}**")
    summary_lines.append(f"- Artifact hash: `{artifact_hash[:12]}…`")
    summary_lines.append(f"- Time (UTC): `{now_iso}`\n")

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Memory Service",
        subdir="validation",
        filename="memory_summary.md",
        content="\n".join(summary_lines),
    )

    state["validation_memory_ingest"] = events
    state["status"] = f"✅ Validation Memory done: facts={len(inserted_facts)} interps={len(interp_payloads)} promotions={promotions}"
    return state
