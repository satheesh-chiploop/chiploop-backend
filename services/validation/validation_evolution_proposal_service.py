# services/validation_evolution_proposal_service.py
from __future__ import annotations

import os
import json
import datetime
import difflib
import asyncio
from typing import Any, Dict, List, Tuple

from utils.artifact_utils import save_text_artifact_and_record
from utils.llm_utils import run_llm_fallback

from services.validation.validation_plan_resolver import load_active_plan_by_name

PROMPT_VERSION = "wf7_v1"
MIN_CONFIDENCE = float(os.getenv("EVOLVE_MIN_CONFIDENCE", "0.5"))
MIN_FAIL_COUNT = int(os.getenv("EVOLVE_MIN_FAIL_COUNT", "2"))
WINDOW_DAYS = int(os.getenv("EVOLVE_WINDOW_DAYS", "30"))


def _utc_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def _diff_text(old: str, new: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="current_test_plan.json",
            tofile="proposed_test_plan.json",
        )
    )


async def _call_llm(prompt: str) -> str:
    out = run_llm_fallback(prompt)
    if asyncio.iscoroutine(out):
        out = await out
    return str(out)


def _get_actionable_failures_from_memory(supabase, bench_id: str, plan_id: str) -> List[Dict[str, Any]]:
    """
    Uses validation_memory as the primary trigger.
    Actionable = recurring (fail_count>=MIN_FAIL_COUNT) OR flaky (fail_count>=1 & pass_count>=1),
    and confidence>=MIN_CONFIDENCE.
    """
    try:
        resp = (
            supabase.table("validation_memory")
            .select("*")
            .eq("bench_id", str(bench_id))
            .eq("test_plan_id", str(plan_id))
            .execute()
        )
        rows = resp.data or []
    except Exception:
        rows = []

    actionable: List[Dict[str, Any]] = []
    for r in rows:
        fail_count = int(r.get("fail_count") or 0)
        pass_count = int(r.get("pass_count") or 0)
        conf = float(r.get("confidence") or 0.0)

        is_recur = fail_count >= MIN_FAIL_COUNT
        is_flaky = (fail_count >= 1 and pass_count >= 1)

        if conf >= MIN_CONFIDENCE and (is_recur or is_flaky):
            actionable.append(r)

    # prioritize most impactful first
    def sev_rank(s: str) -> int:
        m = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        return m.get((s or "low").lower(), 1)

    actionable.sort(
        key=lambda r: (
            sev_rank(r.get("severity")),
            float(r.get("confidence") or 0.0),
            int(r.get("frequency") or 0),
            int(r.get("fail_count") or 0),
        ),
        reverse=True,
    )
    return actionable


def _apply_guardrails(current_plan: Dict[str, Any], proposed_plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    MVP guardrails:
    - must keep dut/tests
    - must not delete all tests
    - must not change dut.name if present
    """
    if not isinstance(proposed_plan, dict):
        raise ValueError("Proposed plan is not a JSON object")

    if "dut" not in proposed_plan or "tests" not in proposed_plan:
        raise ValueError("Proposed plan missing required keys: dut/tests")

    tests = proposed_plan.get("tests")
    if not isinstance(tests, list) or len(tests) == 0:
        raise ValueError("Proposed plan has no tests")

    cur_dut = current_plan.get("dut") if isinstance(current_plan.get("dut"), dict) else {}
    prop_dut = proposed_plan.get("dut") if isinstance(proposed_plan.get("dut"), dict) else {}
    cur_name = (cur_dut.get("name") or "").strip()
    prop_name = (prop_dut.get("name") or "").strip()
    if cur_name and prop_name and cur_name != prop_name:
        raise ValueError(f"DUT name changed: '{cur_name}' -> '{prop_name}'")

    return proposed_plan


async def compute_and_store_evolution_proposal(state: Dict[str, Any], supabase) -> Dict[str, Any]:
    """
    WF7 entrypoint:
    - Resolve active plan by user_id + test_plan_name
    - Gate: actionable failures must exist in validation_memory; otherwise no-op.
    - LLM proposes refined/split diagnostic tests for the failing tests ONLY.
    - Writes proposal artifacts. No DB mutation.
    """
    now = _utc_iso()

    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")
    test_plan_name = (
        state.get("test_plan_name")
        or state.get("validation_test_plan_name")
        or state.get("test_plan")
        or ""
    ).strip()

    # ✅ If workflow_id exists, always produce an artifact explaining what’s missing
    if not workflow_id:
        state["status"] = "❌ WF7 missing workflow_id"
        return state

    missing = []
    if not user_id:
        missing.append("user_id")
    if not bench_id:
        missing.append("bench_id")
    if not test_plan_name:
        missing.append("test_plan_name")

    if missing:
        msg = f"❌ WF7 missing required field(s): {', '.join(missing)}"
        save_text_artifact_and_record(
            workflow_id,
            "Validation Evolution Proposal Agent",
            "validation",
            "evolution_error.md",
            "## Evolution Proposal (Error)\n\n"
            f"- Time: `{now}`\n"
            f"- Missing: `{', '.join(missing)}`\n\n"
            "Fix:\n"
            "- Ensure the UI sends bench_id + test_plan_name, and backend injects user_id.\n",
        )
        state["status"] = msg
        return state

    # ✅ Normalize into one key so downstream has one contract
    state["bench_id"] = str(bench_id)
    state["test_plan_name"] = test_plan_name

    plan_row = load_active_plan_by_name(supabase, str(user_id), test_plan_name)
    if not plan_row:
        msg = f"❌ No active test plan found for user_id={user_id} name='{test_plan_name}'"
        save_text_artifact_and_record(
            workflow_id,
            "Validation Evolution Proposal Agent",
            "validation",
            "evolution_no_action.md",
            f"## Evolution Proposal\n\n- Time: `{now}`\n\n{msg}\n",
        )
        state["status"] = msg
        return state

    plan_id = plan_row.get("id")
    current_plan = plan_row.get("plan_json") if isinstance(plan_row.get("plan_json"), dict) else {}

    actionable = _get_actionable_failures_from_memory(supabase, str(bench_id), str(plan_id))
    if not actionable:
        msg = "✅ No action: no actionable failures found in validation_memory for this bench+plan."
        save_text_artifact_and_record(
            workflow_id,
            "Validation Evolution Proposal Agent",
            "validation",
            "evolution_no_action.md",
            f"## Evolution Proposal\n\n- Time: `{now}`\n- Bench: `{bench_id}`\n- Plan: `{test_plan_name}`\n\n{msg}\n",
        )
        state["status"] = "✅ WF7 no-op (no actionable failures)"
        state["evolution_proposed"] = False
        return state

    # Build evidence summary for LLM
    evidence = []
    for r in actionable[:10]:
        evidence.append(
            {
                "test_name": r.get("test_name"),
                "memory_kind": r.get("memory_kind"),
                "failure_signature": r.get("failure_signature"),
                "failure_category": r.get("failure_category"),
                "severity": r.get("severity"),
                "confidence": float(r.get("confidence") or 0.0),
                "fail_count": int(r.get("fail_count") or 0),
                "pass_count": int(r.get("pass_count") or 0),
                "frequency": int(r.get("frequency") or 0),
                "last_seen": r.get("last_seen"),
                "metadata": r.get("metadata") or {},
            }
        )

    system = """
You are ChipLoop's Validation Evolution Proposal Agent.

Goal:
- Propose diagnostic refinements ONLY for tests implicated by actionable failure evidence.
- Split a failing test into smaller tests if it helps isolate root cause (e.g., ramp vs steady-state).
- Add explicit settling/timeout/retry or additional measurements ONLY when justified.

Constraints:
- Output STRICT JSON only (no markdown outside fields).
- Do NOT change dut.name (if present).
- Keep the plan schema with keys: dut, tests.
- Avoid deleting tests; prefer refine/split/add diagnostics.

Return JSON:
{
  "proposed_plan": { ... },
  "changes": [
    { "type": "split|refine|add", "test_name": "...", "why": "...", "details": { ... } }
  ],
  "rationale_md": "markdown string",
  "risk_notes": ["..."]
}
If no changes are needed, return proposed_plan identical to current and changes=[].
""".strip()

    payload = {
        "prompt_version": PROMPT_VERSION,
        "time_utc": now,
        "bench_id": str(bench_id),
        "test_plan_name": test_plan_name,
        "current_plan": current_plan,
        "actionable_failure_evidence": evidence,
    }

    raw = await _call_llm(system + "\n\nINPUT_JSON:\n" + json.dumps(payload, indent=2))

    # Parse JSON (strict-ish)
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        import re

        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            parsed = json.loads(m.group(0))

    if not isinstance(parsed, dict) or "proposed_plan" not in parsed:
        raise ValueError("WF7 LLM did not return JSON with 'proposed_plan'")

    proposed_plan = parsed.get("proposed_plan")
    changes = parsed.get("changes") or []
    rationale_md = str(parsed.get("rationale_md") or "").strip()

    proposed_plan = _apply_guardrails(current_plan, proposed_plan)

    old_txt = _json(current_plan)
    new_txt = _json(proposed_plan)
    is_identical = (old_txt == new_txt) or (not changes)

    # Always write a status artifact
    status = (
        "## Evolution Proposal\n\n"
        f"- Time: `{now}`\n"
        f"- Bench: `{bench_id}`\n"
        f"- Plan: `{test_plan_name}`\n"
        f"- Actionable failures: `{len(actionable)}`\n\n"
    )
    status += "✅ No changes proposed.\n" if is_identical else "✅ Proposal generated.\n"
    save_text_artifact_and_record(
        workflow_id,
        "Validation Evolution Proposal Agent",
        "validation",
        "evolution_status.md",
        status,
    )

    if is_identical:
        state["status"] = "✅ WF7 completed (no changes proposed)"
        state["evolution_proposed"] = False
        return state

    # Proposal artifacts
    save_text_artifact_and_record(
        workflow_id,
        "Validation Evolution Proposal Agent",
        "validation",
        "proposed_test_plan.json",
        new_txt,
    )
    save_text_artifact_and_record(
        workflow_id,
        "Validation Evolution Proposal Agent",
        "validation",
        "evolution_plan_diff.md",
        "```diff\n" + _diff_text(old_txt, new_txt) + "\n```",
    )
    if not rationale_md:
        rationale_md = "### Rationale\n\nProposal generated from repeated/flaky failure evidence.\n"
    save_text_artifact_and_record(
        workflow_id,
        "Validation Evolution Proposal Agent",
        "validation",
        "evolution_rationale.md",
        rationale_md,
    )

    # Put minimal fields back into state for WF9 Apply
    state["status"] = "✅ WF7 proposal generated"
    state["evolution_proposed"] = True
    state["proposal_kind"] = "evolution"
    state["proposed_test_plan"] = proposed_plan
    return state
