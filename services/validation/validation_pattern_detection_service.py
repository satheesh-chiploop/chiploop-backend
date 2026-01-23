# services/validation_pattern_detection_service.py

from __future__ import annotations

import os
import json
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
    return datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc
    ).isoformat()


# ======================================================
# ✅ NEW: resolve test_plan_name → test_plan_id
# ======================================================
def _resolve_test_plan_id_by_name(
    supabase, test_plan_name: str, user_id: Optional[str]
) -> Optional[str]:

    q = (
        supabase.table("validation_test_plans")
        .select("id,created_at,is_active")
        .eq("name", test_plan_name)
    )

    if user_id:
        q = q.eq("user_id", user_id)

    resp = (
        q.order("is_active", desc=True)
         .order("created_at", desc=True)
         .limit(1)
         .execute()
    )

    rows = resp.data or []
    if not rows:
        return None

    return str(rows[0].get("id"))


# ======================================================
# MAIN ENTRY
# ======================================================
async def compute_and_store_validation_patterns(
    state: Dict[str, Any], supabase
) -> Dict[str, Any]:

    workflow_id = state.get("workflow_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")

    test_plan_name = (
        state.get("test_plan_name")
        or state.get("validation_test_plan_name")
    )

    user_id = state.get("user_id")

    if not workflow_id:
        state["status"] = "❌ WF6: missing workflow_id"
        return state

    if not bench_id or not test_plan_name:
        state["status"] = "❌ WF6: missing bench_id or test_plan_name"
        return state

    # resolve id internally (facts table still keyed by id)
    test_plan_id = _resolve_test_plan_id_by_name(
        supabase, test_plan_name, user_id
    )

    now_iso = _utcnow_iso()
    window_days = int(state.get("pattern_window_days") or WINDOW_DAYS)

    # --------------------------------------------------
    # if plan not found → NO-OP artifact (very important)
    # --------------------------------------------------
    if not test_plan_id:
        patterns = {
            "timestamp": now_iso,
            "bench_id": str(bench_id),
            "test_plan_name": test_plan_name,
            "test_plan_id": None,
            "window_days": window_days,
            "facts_considered": 0,
            "patterns": {
                "recurring_clusters": [],
                "flaky_tests": [],
                "correlations": [],
            },
            "no_action_reason": "test_plan_name not found in saved plans",
        }

        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Pattern Detection Agent",
            subdir="validation",
            filename="patterns.json",
            content=json.dumps(patterns, indent=2),
        )

        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Pattern Detection Agent",
            subdir="validation",
            filename="patterns_no_action.md",
            content=(
                "# Validation Pattern Detection\n\n"
                f"- Bench: `{bench_id}`\n"
                f"- Test plan: `{test_plan_name}`\n\n"
                "No historical runs were found for this test plan.\n"
            ),
        )

        state["validation_patterns"] = patterns
        state["status"] = "🟡 WF6: no-op (plan not found)"
        return state

    # --------------------------------------------------
    # normal path
    # --------------------------------------------------
    from services.validation_pattern_detection_service import (
        _get_fact_window,
        _get_interpretations_for_fact_ids,
        _detect_recurring_clusters,
        _detect_flaky_tests,
        _detect_correlations,
    )

    facts = _get_fact_window(
        supabase,
        str(bench_id),
        str(test_plan_id),
        window_days=window_days,
    )

    fact_ids = [str(f.get("id")) for f in facts if f.get("id")]
    interp_by_fact = _get_interpretations_for_fact_ids(supabase, fact_ids)

    recurring = _detect_recurring_clusters(facts, interp_by_fact)
    flaky = _detect_flaky_tests(facts)
    corr = _detect_correlations(facts)

    patterns = {
        "timestamp": now_iso,
        "bench_id": str(bench_id),
        "test_plan_name": test_plan_name,
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

    # --------------------------------------------------
    # summary
    # --------------------------------------------------
    lines = [
        "# Validation Pattern Detection\n",
        f"- Bench: `{bench_id}`",
        f"- Test plan: `{test_plan_name}`",
        f"- Window: last **{window_days} days**",
        f"- Facts considered: **{len(facts)}**",
        f"- Time (UTC): `{now_iso}`\n",
    ]

    def _render(title, items):
        lines.append(f"## {title}\n")
        if not items:
            lines.append("- (none)\n")
            return
        for i, it in enumerate(items[:6], start=1):
            lines.append(f"### {i}. {it.get('title')}")
            lines.append(f"- Summary: {it.get('summary')}")
            lines.append(
                f"- Severity: **{it.get('severity','low')}** | "
                f"Confidence: **{float(it.get('confidence', 0.5)):.2f}**"
            )
            lines.append("")

    _render("Recurring Failure Clusters", recurring)
    _render("Flaky Tests", flaky)
    _render("Correlations", corr)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Pattern Detection Agent",
        subdir="validation",
        filename="patterns_summary.md",
        content="\n".join(lines),
    )

    # --------------------------------------------------
    # explicit no-pattern artifact (GOLDEN LIST)
    # --------------------------------------------------
    if not recurring and not flaky and not corr:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Pattern Detection Agent",
            subdir="validation",
            filename="patterns_no_action.md",
            content=(
                "# Validation Pattern Detection\n\n"
                "No recurring patterns, flaky tests, or correlations were detected "
                "for this bench and test plan in the selected window.\n"
            ),
        )

    state["validation_patterns"] = patterns
    state["status"] = "✅ WF6 Pattern Detection completed"
    return state

