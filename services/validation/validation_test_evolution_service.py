# services/validation_test_evolution_service.py
#
# Phase 3: Test Plan Evolution (proposal only)
# - Reads: validation_test_plans (plan_json), validation_memory (+ optionally run facts later)
# - Writes artifacts:
#     validation/evolution_status.md   (always)
#     validation/proposed_test_plan.json (only if actionable failure exists)
#     validation/evolution_diff.md       (only if actionable failure exists)
#     validation/evolution_rationale.md  (only if actionable failure exists)
#
# GUARANTEE: If no actionable failure -> no proposal, no plan mutation.
#
from __future__ import annotations

import json
import os
import datetime
import difflib
import asyncio
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record
from utils.llm_utils import run_llm_fallback  # your existing helper (may be async)

PROMPT_VERSION = "v1"
MIN_CONFIDENCE = float(os.getenv("EVOLVE_MIN_CONFIDENCE", "0.5"))
MIN_FAIL_COUNT = int(os.getenv("EVOLVE_MIN_FAIL_COUNT", "2"))


def _utc_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _safe(obj: Any, default):
    return obj if obj is not None else default


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)


async def _call_llm(prompt: str) -> str:
    out = run_llm_fallback(prompt)
    if asyncio.iscoroutine(out):
        out = await out
    return str(out)


def _load_test_plan_json(supabase, test_plan_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = (
            supabase.table("validation_test_plans")
            .select("id,name,plan_json,description")
            .eq("id", test_plan_id)
            .single()
            .execute()
        )
        row = resp.data or None
        if not row:
            return None
        plan = row.get("plan_json")
        if isinstance(plan, dict):
            return {"row": row, "plan_json": plan}
        return None
    except Exception:
        return None


def _get_actionable_memory(supabase, bench_id: str, test_plan_id: str) -> List[Dict[str, Any]]:
    """
    Returns memory rows that qualify for evolution triggers.
    We keep this strict and safe.
    """
    try:
        resp = (
            supabase.table("validation_memory")
            .select("*")
            .eq("bench_id", bench_id)
            .eq("test_plan_id", test_plan_id)
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

        is_repeated_failure = fail_count >= MIN_FAIL_COUNT
        is_flaky = (fail_count >= 1 and pass_count >= 1)

        if conf >= MIN_CONFIDENCE and (is_repeated_failure or is_flaky):
            actionable.append(r)

    # prioritize by severity/confidence/frequency
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


def _diff_text(old: str, new: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile="current_test_plan.json", tofile="proposed_test_plan.json"))


def _apply_guardrails(current_plan: Dict[str, Any], proposed_plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic safety:
    - Must remain a dict with dut/tests
    - Must not remove dut
    - Must not remove all tests
    - Must not change DUT name (if present)
    - Must not introduce new instrument types outside {psu,dmm,scope} (from your schema)
    """
    if not isinstance(proposed_plan, dict):
        raise ValueError("Proposed plan is not a JSON object")

    if "dut" not in proposed_plan or "tests" not in proposed_plan:
        raise ValueError("Proposed plan missing required keys: dut/tests")

    if not isinstance(proposed_plan.get("tests"), list) or len(proposed_plan["tests"]) == 0:
        raise ValueError("Proposed plan has no tests")

    # DUT name must not change (if present)
    cur_dut = current_plan.get("dut") if isinstance(current_plan.get("dut"), dict) else {}
    prop_dut = proposed_plan.get("dut") if isinstance(proposed_plan.get("dut"), dict) else {}
    cur_name = (cur_dut.get("name") or "").strip()
    prop_name = (prop_dut.get("name") or "").strip()
    if cur_name and prop_name and cur_name != prop_name:
        raise ValueError(f"DUT name changed: '{cur_name}' -> '{prop_name}'")

    # instrument type check
    allowed = {"psu", "dmm", "scope"}
    for t in proposed_plan.get("tests", []):
        req = t.get("required_instruments") or []
        if isinstance(req, list):
            for it in req:
                s = str(it).strip().lower()
                if s and s not in allowed:
                    raise ValueError(f"Proposed plan introduced unsupported instrument type: {s}")

    return proposed_plan


async def compute_and_store_test_evolution(state: Dict[str, Any], supabase) -> Dict[str, Any]:
    """
    WF6 entrypoint (bench-only cognition).
    Requires:
      - workflow_id
      - bench_id
      - test_plan_id
      - user_id (optional; used only if you later want to store proposed plan)
    """
    workflow_id = state.get("workflow_id")
    bench_id = state.get("bench_id") or state.get("validation_bench_id")
    test_plan_id = state.get("test_plan_id") or state.get("validation_test_plan_id")

    now = _utc_iso()

    if not workflow_id:
        state["status"] = "❌ Test Evolution: missing workflow_id"
        return state
    if not bench_id or not test_plan_id:
        state["status"] = "❌ Test Evolution: missing bench_id or test_plan_id"
        return state

    # 1) Load current plan
    loaded = _load_test_plan_json(supabase, str(test_plan_id))
    if not loaded:
        msg = f"No action: could not load test plan id {test_plan_id}"
        save_text_artifact_and_record(workflow_id, "Validation Test Evolution Agent", "validation", "evolution_status.md", f"## Test Evolution\n\n- Time: `{now}`\n- {msg}\n")
        state["status"] = f"✅ Test Evolution skipped (missing plan)"
        return state

    current_plan = loaded["plan_json"]
    plan_row = loaded["row"]

    # 2) Check actionable failures (HARD NO-OP if none)
    actionable = _get_actionable_memory(supabase, str(bench_id), str(test_plan_id))
    if not actionable:
        msg = "No action: no actionable failures found (no recurring/flaky failure evidence)."
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Validation Test Evolution Agent",
            subdir="validation",
            filename="evolution_status.md",
            content=f"## Test Evolution\n\n- Time: `{now}`\n- Bench: `{bench_id}`\n- Test plan: `{test_plan_id}`\n\n✅ {msg}\n",
        )
        state["status"] = "✅ Test Evolution skipped (no failure evidence)"
        state["evolution_applied"] = False
        return state

    # 3) Build evolution context (only based on failing/flaky tests)
    # Keep it small, evidence-backed.
    top_items = actionable[:8]
    evidence_summary = []
    for r in top_items:
        evidence_summary.append({
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
        })

    # 4) Ask LLM for a PROPOSAL ONLY (strict JSON)
    system = """
You are ChipLoop's Validation Test Plan Evolution Agent.

You will receive:
- Current test plan JSON (dut/tests schema)
- Actionable failure evidence (recurring/flaky) for this SAME bench and test plan lineage

You MUST:
- Propose modifications ONLY to tests implicated by failure evidence (matching test_name).
- Keep the DUT name unchanged.
- Do NOT introduce new instrument types (allowed: psu, dmm, scope).
- Do NOT delete tests unless absolutely necessary; prefer split/refine/annotate.
- Output STRICT JSON only (no markdown).

Return JSON with keys:
{
  "proposed_plan": { ...same schema as current plan... },
  "changes": [
     { "type": "...", "test_name": "...", "field": "...", "from": "...", "to": "...", "why": "..." }
  ],
  "rationale_md": "markdown string explaining why these changes help",
  "risk_notes": [ "...", "..." ]
}

If the best action is to NOT change the plan, return proposed_plan identical to current and changes=[].
"""

    user = {
        "bench_id": str(bench_id),
        "test_plan_id": str(test_plan_id),
        "current_plan_name": plan_row.get("name"),
        "current_plan": current_plan,
        "actionable_failure_evidence": evidence_summary,
        "prompt_version": PROMPT_VERSION,
        "time_utc": now,
    }

    raw = await _call_llm(system + "\n\nINPUT_JSON:\n" + json.dumps(user, indent=2))

    # Parse JSON strictly / best-effort
    proposed = None
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        # try extracting JSON object
        import re
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            parsed = json.loads(m.group(0))

    if not isinstance(parsed, dict) or "proposed_plan" not in parsed:
        raise ValueError("LLM did not return valid evolution JSON with proposed_plan")

    proposed = parsed.get("proposed_plan")
    changes = parsed.get("changes") or []
    rationale_md = str(parsed.get("rationale_md") or "").strip()
    risk_notes = parsed.get("risk_notes") or []

    # 5) Guardrails (hard)
    proposed = _apply_guardrails(current_plan, proposed)

    # 6) If LLM returned no changes (or identical plan), still write status + rationale
    old_txt = _json(current_plan)
    new_txt = _json(proposed)
    is_identical = (old_txt == new_txt) or (not changes)

    # Always write status
    status_lines = [
        "## Test Evolution",
        f"- Time: `{now}`",
        f"- Bench: `{bench_id}`",
        f"- Test plan: `{test_plan_id}`",
        "",
    ]
    if is_identical:
        status_lines.append("✅ No action: evolution proposal resulted in no changes.")
    else:
        status_lines.append("✅ Proposal generated: review proposed plan + diff artifacts.")

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Test Evolution Agent",
        subdir="validation",
        filename="evolution_status.md",
        content="\n".join(status_lines) + "\n",
    )

    # If identical/no changes, stop here (still no mutation)
    if is_identical:
        state["status"] = "✅ Test Evolution no-op (no changes proposed)"
        state["evolution_applied"] = False
        return state

    # 7) Write proposal artifacts
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Test Evolution Agent",
        subdir="validation",
        filename="proposed_test_plan.json",
        content=new_txt,
    )

    diff = _diff_text(old_txt, new_txt)
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Test Evolution Agent",
        subdir="validation",
        filename="evolution_diff.md",
        content="```diff\n" + diff + "\n```",
    )

    if not rationale_md:
        # fallback rationale
        rationale_md = "### Rationale\n\nThis proposal was generated based on repeated/flaky failures observed for the same bench and test plan.\n"
        if isinstance(changes, list) and changes:
            rationale_md += "\n### Proposed Changes\n"
            for c in changes[:20]:
                rationale_md += f"- {c}\n"
        if isinstance(risk_notes, list) and risk_notes:
            rationale_md += "\n### Risk Notes\n"
            for r in risk_notes[:10]:
                rationale_md += f"- {r}\n"

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Validation Test Evolution Agent",
        subdir="validation",
        filename="evolution_rationale.md",
        content=rationale_md,
    )

    # No DB mutation in Phase 3 by default
    state["status"] = "✅ Test Evolution proposal generated"
    state["evolution_applied"] = False
    state["proposed_test_plan"] = proposed
    state["evolution_changes"] = changes
    return state
