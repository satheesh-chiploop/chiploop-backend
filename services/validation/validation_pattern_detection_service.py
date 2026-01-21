# services/validation_pattern_detection_service.py
#
# ChipLoop Cognition: Validation Pattern Detection (WF5)
#
# Reads Phase-1 tables:
#   - validation_run_facts
#   - validation_run_interpretations
#
# Writes workflow artifacts:
#   - validation/patterns.json
#   - validation/patterns_summary.md
#
from __future__ import annotations

import os
import json
import math
import datetime
import asyncio
from typing import Any, Dict, List, Optional, Tuple, Set

from utils.artifact_utils import save_text_artifact_and_record
from utils.llm_utils import run_llm_fallback

PROMPT_VERSION = "v1"
MAX_FACTS = int(os.getenv("PATTERN_MAX_FACTS", "400"))
WINDOW_DAYS = int(os.getenv("PATTERN_WINDOW_DAYS", "30"))
TOP_K_LLM = int(os.getenv("PATTERN_TOP_K_LLM", "6"))


def _utcnow_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _safe_json_load(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            obj = json.loads(x)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


async def _call_llm(prompt: str) -> str:
    out = run_llm_fallback(prompt)
    if asyncio.iscoroutine(out):
        out = await out
    return str(out)


def _is_fail(status: str) -> bool:
    s = (status or "").strip().lower()
    return s in ("fail", "failed", "error")


def _is_pass(status: str) -> bool:
    s = (status or "").strip().lower()
    return s in ("pass", "passed", "ok", "success")


def _get_fact_window(supabase, bench_id: str, test_plan_id: str, window_days: int) -> List[Dict[str, Any]]:
    # Prefer time filter; fallback to MAX_FACTS ordering
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=window_days)).replace(tzinfo=datetime.timezone.utc).isoformat()
    resp = (
        supabase.table("validation_run_facts")
        .select("*")
        .eq("bench_id", bench_id)
        .eq("test_plan_id", test_plan_id)
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(MAX_FACTS)
        .execute()
    )
    return resp.data or []


def _get_interpretations_for_fact_ids(supabase, fact_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not fact_ids:
        return {}
    # Supabase "in_" works with list
    resp = (
        supabase.table("validation_run_interpretations")
        .select("*")
        .in_("fact_id", fact_ids)
        .order("created_at", desc=True)
        .execute()
    )
    rows = resp.data or []
    # Keep newest interpretation per fact_id
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        fid = r.get("fact_id")
        if fid and fid not in out:
            out[str(fid)] = r
    return out


def _severity_rank(sev: str) -> int:
    s = (sev or "").lower()
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(s, 1)


def _score_pattern(support: int, avg_conf: float, severity: str) -> float:
    return float(support) * float(avg_conf) * float(_severity_rank(severity))


async def _llm_enrich_pattern(cluster: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enriches a candidate pattern (already deterministically detected) with:
    - title
    - summary
    - recommendations
    - confidence/severity (bounded)
    """
    system = """
You are ChipLoop's Validation Pattern Analyst.

You will be given an evidence-backed detected pattern.
DO NOT invent data.
Return STRICT JSON only with keys:
title, summary, recommendations (array), confidence (0..1), severity (low|medium|high|critical)

If evidence is weak, keep confidence <= 0.5 and severity <= medium.
"""
    user = f"""
EVIDENCE (JSON):
{json.dumps(cluster, indent=2)}

Return STRICT JSON only.
"""
    raw = await _call_llm(system + "\n" + user)
    data = _safe_json_load(raw)

    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    recs = data.get("recommendations") or []
    conf = data.get("confidence")
    sev = str(data.get("severity") or "").strip().lower()

    if not title:
        title = cluster.get("title") or "Detected validation pattern"
    if not summary:
        summary = cluster.get("summary") or "Pattern detected from repeated evidence."
    if not isinstance(recs, list):
        recs = []
    try:
        conf = float(conf)
    except Exception:
        conf = float(cluster.get("avg_confidence") or 0.5)
    conf = max(0.0, min(1.0, conf))
    if sev not in ("low", "medium", "high", "critical"):
        sev = str(cluster.get("severity") or "low")

    return {
        "title": title,
        "summary": summary,
        "recommendations": recs[:8],
        "confidence": conf,
        "severity": sev,
    }


def _detect_recurring_clusters(facts: List[Dict[str, Any]], interp_by_fact: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    clusters: Dict[str, Dict[str, Any]] = {}
    for f in facts:
        if not _is_fail(f.get("status")):
            continue
        sig = (f.get("failure_signature") or "").strip()
        if not sig:
            continue

        fid = str(f.get("id") or "")
        interp = interp_by_fact.get(fid) or {}
        cat = interp.get("failure_category") or "unknown"
        conf = float(interp.get("confidence") or 0.3)
        sev = str(interp.get("severity") or "low")

        c = clusters.get(sig)
        if not c:
            clusters[sig] = {
                "pattern_kind": "recurring_cluster",
                "failure_signature": sig,
                "support_count": 0,
                "avg_confidence": 0.0,
                "severity": "low",
                "categories": {},
                "test_names": set(),
                "evidence_run_ids": set(),
                "evidence_fact_ids": set(),
                "signals_used": set(),
            }
            c = clusters[sig]

        c["support_count"] += 1
        c["avg_confidence"] += conf
        # keep max severity
        if _severity_rank(sev) > _severity_rank(c["severity"]):
            c["severity"] = sev

        c["categories"][cat] = int(c["categories"].get(cat, 0)) + 1
        c["test_names"].add(str(f.get("test_name") or "unnamed_test"))
        c["evidence_run_ids"].add(str(f.get("run_id") or ""))
        c["evidence_fact_ids"].add(str(f.get("id") or ""))
        for s in (interp.get("signals_used") or []):
            if isinstance(s, str) and s.strip():
                c["signals_used"].add(s.strip())

    out: List[Dict[str, Any]] = []
    for sig, c in clusters.items():
        support = int(c["support_count"])
        if support <= 1:
            continue  # promote only if repeated
        avg_conf = float(c["avg_confidence"]) / float(max(1, support))
        dominant_cat = "unknown"
        if isinstance(c["categories"], dict) and c["categories"]:
            dominant_cat = sorted(c["categories"].items(), key=lambda kv: kv[1], reverse=True)[0][0]

        out.append({
            "pattern_kind": "recurring_cluster",
            "failure_signature": sig,
            "support_count": support,
            "avg_confidence": round(avg_conf, 3),
            "severity": c["severity"],
            "dominant_category": dominant_cat,
            "categories": c["categories"],
            "test_names": sorted(list(c["test_names"]))[:25],
            "signals_used": sorted(list(c["signals_used"]))[:25],
            "evidence_run_ids": sorted([x for x in c["evidence_run_ids"] if x])[:40],
            "evidence_fact_ids": sorted([x for x in c["evidence_fact_ids"] if x])[:60],
            "title": f"Recurring failure signature {sig[:10]}… ({support} occurrences)",
            "summary": f"Repeated failures with signature {sig}. Dominant category: {dominant_cat}.",
        })

    # rank
    out.sort(key=lambda x: _score_pattern(x["support_count"], x["avg_confidence"], x["severity"]), reverse=True)
    return out


def _detect_flaky_tests(facts: List[Dict[str, Any]], min_total: int = 6) -> List[Dict[str, Any]]:
    # Use per-test status rows
    by_test: Dict[str, Dict[str, Any]] = {}
    for f in facts:
        tn = str(f.get("test_name") or "unnamed_test")
        st = (f.get("status") or "").lower()
        if tn not in by_test:
            by_test[tn] = {"pass": 0, "fail": 0, "runs": set(), "fact_ids": set()}
        if _is_pass(st):
            by_test[tn]["pass"] += 1
        elif _is_fail(st):
            by_test[tn]["fail"] += 1
        by_test[tn]["runs"].add(str(f.get("run_id") or ""))
        by_test[tn]["fact_ids"].add(str(f.get("id") or ""))

    out: List[Dict[str, Any]] = []
    for tn, agg in by_test.items():
        p = int(agg["pass"])
        f = int(agg["fail"])
        total = p + f
        if total < min_total:
            continue
        fail_rate = f / float(total)
        if f >= 2 and p >= 2 and 0.2 <= fail_rate <= 0.8:
            out.append({
                "pattern_kind": "flaky_cluster",
                "test_name": tn,
                "support_count": total,
                "fail_count": f,
                "pass_count": p,
                "fail_rate": round(fail_rate, 3),
                "avg_confidence": 0.7,
                "severity": "medium",
                "evidence_run_ids": sorted([x for x in agg["runs"] if x])[:40],
                "evidence_fact_ids": sorted([x for x in agg["fact_ids"] if x])[:60],
                "title": f"Flaky test: {tn}",
                "summary": f"Test {tn} shows pass/fail oscillation (fail_rate={fail_rate:.2f}) over {total} samples.",
            })
    out.sort(key=lambda x: (x["fail_rate"], x["support_count"]), reverse=True)
    return out


def _kv_flatten(obj: Any, prefix: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kk = f"{prefix}.{k}"
            if isinstance(v, (str, int, float, bool)) and v is not None:
                out.append((kk, str(v)))
            elif isinstance(v, dict):
                out.extend(_kv_flatten(v, kk))
            elif isinstance(v, list):
                # list -> join
                vals = []
                for it in v:
                    if isinstance(it, (str, int, float, bool)) and it is not None:
                        vals.append(str(it))
                if vals:
                    out.append((kk, ",".join(vals[:12])))
    return out


def _detect_correlations(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Simple lift-based correlation on:
      - conditions (env/mode/temp)
      - instrument_refs
      - dut_refs
    """
    total = 0
    fail_total = 0

    # counts for feature=value
    feat_all: Dict[str, int] = {}
    feat_fail: Dict[str, int] = {}

    evidence_runs: Dict[str, Set[str]] = {}
    evidence_facts: Dict[str, Set[str]] = {}

    for f in facts:
        total += 1
        is_fail = _is_fail(f.get("status"))
        if is_fail:
            fail_total += 1

        fid = str(f.get("id") or "")
        rid = str(f.get("run_id") or "")

        features: List[Tuple[str, str]] = []
        features += _kv_flatten(f.get("conditions") or {}, "cond")
        features += _kv_flatten(f.get("instrument_refs") or {}, "inst")
        features += _kv_flatten(f.get("dut_refs") or {}, "dut")

        for k, v in features:
            key = f"{k}={v}"
            feat_all[key] = feat_all.get(key, 0) + 1
            if is_fail:
                feat_fail[key] = feat_fail.get(key, 0) + 1
                evidence_runs.setdefault(key, set()).add(rid)
                evidence_facts.setdefault(key, set()).add(fid)

    out: List[Dict[str, Any]] = []
    if total < 10 or fail_total < 3:
        return out

    base_fail_rate = fail_total / float(total)

    for key, n_all in feat_all.items():
        n_fail = feat_fail.get(key, 0)
        if n_all < 4 or n_fail < 3:
            continue

        fail_rate_feat = n_fail / float(n_all)
        lift = fail_rate_feat / float(base_fail_rate) if base_fail_rate > 0 else 0.0

        # only keep meaningful correlations
        if lift >= 1.8 and fail_rate_feat >= 0.6:
            out.append({
                "pattern_kind": "correlation",
                "feature": key,
                "support_count": n_all,
                "fail_count": n_fail,
                "fail_rate": round(fail_rate_feat, 3),
                "lift": round(lift, 3),
                "avg_confidence": 0.6,
                "severity": "medium",
                "evidence_run_ids": sorted([x for x in evidence_runs.get(key, set()) if x])[:40],
                "evidence_fact_ids": sorted([x for x in evidence_facts.get(key, set()) if x])[:60],
                "title": f"Correlation: {key}",
                "summary": f"Failures are enriched when {key} (lift={lift:.2f}, fail_rate={fail_rate_feat:.2f}).",
            })

    out.sort(key=lambda x: (x["lift"], x["fail_count"]), reverse=True)
    return out[:20]


async def compute_and_store_validation_patterns(state: Dict[str, Any], supabase) -> Dict[str, Any]:
    """
    WF5 entrypoint.
    Expects state has:
      - workflow_id
      - bench_id
      - test_plan_id
      - (optional) pattern_window_days
    """
    workflow_id = state.get("workflow_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")
    test_plan_id = state.get("test_plan_id") or state.get("validation_test_plan_id")

    if not workflow_id:
        state["status"] = "❌ Pattern Detection: missing workflow_id"
        return state
    if not bench_id or not test_plan_id:
        state["status"] = "❌ Pattern Detection: missing bench_id or test_plan_id"
        return state

    window_days = int(state.get("pattern_window_days") or WINDOW_DAYS)
    now_iso = _utcnow_iso()

    facts = _get_fact_window(supabase, str(bench_id), str(test_plan_id), window_days=window_days)
    fact_ids = [str(f.get("id")) for f in facts if f.get("id")]
    interp_by_fact = _get_interpretations_for_fact_ids(supabase, fact_ids)

    recurring = _detect_recurring_clusters(facts, interp_by_fact)
    flaky = _detect_flaky_tests(facts)
    corr = _detect_correlations(facts)

    # LLM enrich top patterns only (safe + cheap)
    enrich_candidates = (recurring[:TOP_K_LLM] + flaky[:max(0, TOP_K_LLM - len(recurring[:TOP_K_LLM]))])
    enriched_map: Dict[str, Dict[str, Any]] = {}
    for c in enrich_candidates:
        key = c.get("failure_signature") or c.get("test_name") or c.get("feature") or c.get("title")
        if not key:
            continue
        enriched_map[str(key)] = await _llm_enrich_pattern(c)

    def _apply_enrich(x: Dict[str, Any]) -> Dict[str, Any]:
        key = x.get("failure_signature") or x.get("test_name") or x.get("feature") or x.get("title")
        e = enriched_map.get(str(key)) if key else None
        if e:
            x["title"] = e["title"]
            x["summary"] = e["summary"]
            x["recommendations"] = e["recommendations"]
            x["confidence"] = e["confidence"]
            x["severity"] = e["severity"]
        else:
            x.setdefault("recommendations", [])
            x.setdefault("confidence", x.get("avg_confidence", 0.5))
        return x

    recurring = [_apply_enrich(x) for x in recurring]
    flaky = [_apply_enrich(x) for x in flaky]
    corr = [_apply_enrich(x) for x in corr]

    patterns = {
        "timestamp": now_iso,
        "bench_id": str(bench_id),
        "test_plan_id": str(test_plan_id),
        "window_days": window_days,
        "facts_considered": len(facts),
        "patterns": {
            "recurring_clusters": recurring,
            "flaky_tests": flaky,
            "correlations": corr,
        },
    }

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Pattern Detection Agent",
        subdir="validation",
        filename="patterns.json",
        content=json.dumps(patterns, indent=2),
    )

    # Markdown summary
    lines: List[str] = []
    lines.append("# Validation Pattern Detection\n")
    lines.append(f"- Bench: `{bench_id}`")
    lines.append(f"- Test plan: `{test_plan_id}`")
    lines.append(f"- Window: last **{window_days} days**")
    lines.append(f"- Facts considered: **{len(facts)}**")
    lines.append(f"- Time (UTC): `{now_iso}`\n")

    def _render_section(title: str, items: List[Dict[str, Any]], max_items: int = 6):
        lines.append(f"## {title}\n")
        if not items:
            lines.append("- (none)\n")
            return
        for i, it in enumerate(items[:max_items], start=1):
            lines.append(f"### {i}. {it.get('title')}")
            lines.append(f"- Summary: {it.get('summary')}")
            lines.append(f"- Severity: **{it.get('severity','low')}** | Confidence: **{float(it.get('confidence') or it.get('avg_confidence') or 0.0):.2f}**")
            if it.get("support_count") is not None:
                lines.append(f"- Support: **{it.get('support_count')}**")
            if it.get("dominant_category"):
                lines.append(f"- Category: `{it.get('dominant_category')}`")
            recs = it.get("recommendations") or []
            if recs:
                lines.append("- Recommendations:")
                for r in recs[:5]:
                    lines.append(f"  - {r}")
            lines.append("")

    _render_section("Top Recurring Failure Clusters", recurring, 6)
    _render_section("Flaky Tests", flaky, 6)
    _render_section("Strong Correlations", corr, 6)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Pattern Detection Agent",
        subdir="validation",
        filename="patterns_summary.md",
        content="\n".join(lines),
    )

    state["validation_patterns"] = patterns
    state["status"] = f"✅ Pattern Detection done: recurring={len(recurring)} flaky={len(flaky)} corr={len(corr)}"
    return state
