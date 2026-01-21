# services/validation_coverage_proposal_service.py
from __future__ import annotations

import os
import json
import datetime
import difflib
import asyncio
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record
from utils.llm_utils import run_llm_fallback

from services.validation.validation_plan_resolver import load_active_plan_by_name

PROMPT_VERSION = "wf8_v1"
WINDOW_DAYS = int(os.getenv("COVERAGE_WINDOW_DAYS", "30"))
MIN_SAMPLES_WEAK = int(os.getenv("COVERAGE_WEAK_MIN_SAMPLES", "2"))


def _utc_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def _diff_text(old: str, new: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="current_test_plan.json", tofile="proposed_test_plan.json")
    )


async def _call_llm(prompt: str) -> str:
    out = run_llm_fallback(prompt)
    if asyncio.iscoroutine(out):
        out = await out
    return str(out)


def _since_ts(days: int) -> str:
    dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.isoformat()


def _load_run_facts(supabase, bench_id: str, plan_id: str, since_iso: str) -> List[Dict[str, Any]]:
    """
    Best-effort: prefer filtering by bench_id + test_plan_id + created_at>=since.
    """
    try:
        resp = (
            supabase.table("validation_run_facts")
            .select("*")
            .eq("bench_id", str(bench_id))
            .eq("test_plan_id", str(plan_id))
            .gte("created_at", since_iso)
            .execute()
        )
        return resp.data or []
    except Exception:
        # fallback: bench-only (older data)
        try:
            resp = (
                supabase.table("validation_run_facts")
                .select("*")
                .eq("bench_id", str(bench_id))
                .gte("created_at", since_iso)
                .execute()
            )
            return resp.data or []
        except Exception:
            return []


def _plan_tests(plan_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    tests = plan_json.get("tests") if isinstance(plan_json.get("tests"), list) else []
    return [t for t in tests if isinstance(t, dict)]


def _test_name(t: Dict[str, Any]) -> str:
    return (t.get("name") or t.get("test_name") or t.get("id") or "").strip() or "unnamed_test"


def _extract_rails_from_dut(plan_json: Dict[str, Any]) -> List[str]:
    dut = plan_json.get("dut") if isinstance(plan_json.get("dut"), dict) else {}
    rails = dut.get("rails")
    out: List[str] = []
    if isinstance(rails, list):
        for r in rails:
            if isinstance(r, str) and r.strip():
                out.append(r.strip())
            elif isinstance(r, dict):
                nm = (r.get("name") or "").strip()
                if nm:
                    out.append(nm)
    elif isinstance(rails, dict):
        for k in rails.keys():
            if str(k).strip():
                out.append(str(k).strip())
    return sorted(list(dict.fromkeys(out)))


def _extract_rails_referenced_by_test(t: Dict[str, Any]) -> List[str]:
    """
    Best-effort: checks common keys.
    """
    out: List[str] = []
    for key in ("rails", "rail", "power_rails", "monitored_rails"):
        v = t.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())

    # Also scan measurements for rail field
    meas = t.get("measurements")
    if isinstance(meas, list):
        for m in meas:
            if isinstance(m, dict):
                rr = m.get("rail") or m.get("rail_name")
                if isinstance(rr, str) and rr.strip():
                    out.append(rr.strip())

    return sorted(list(dict.fromkeys(out)))


def _build_coverage(plan_json: Dict[str, Any], facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    planned = _plan_tests(plan_json)
    planned_names = [_test_name(t) for t in planned]

    # Observed: count by test_name + status
    observed = {}
    for f in facts:
        tn = (f.get("test_name") or "").strip()
        if not tn:
            continue
        status = (f.get("status") or "").strip().lower()  # pass/fail
        o = observed.setdefault(tn, {"samples": 0, "pass": 0, "fail": 0, "run_ids": set()})
        o["samples"] += 1
        if status == "pass":
            o["pass"] += 1
        elif status == "fail":
            o["fail"] += 1
        rid = f.get("run_id")
        if rid:
            o["run_ids"].add(rid)

    # Summaries
    planned_not_run = [n for n in planned_names if n not in observed]
    weak = []
    for n, o in observed.items():
        if o["samples"] < MIN_SAMPLES_WEAK:
            weak.append({"test_name": n, **{k: (list(v) if k == "run_ids" else v) for k, v in o.items()}})

    # Rail coverage
    dut_rails = _extract_rails_from_dut(plan_json)
    rails_referenced = set()
    for t in planned:
        for r in _extract_rails_referenced_by_test(t):
            rails_referenced.add(r)
    rail_gaps = [r for r in dut_rails if r not in rails_referenced] if dut_rails else []

    return {
        "planned_test_count": len(planned_names),
        "observed_test_count": len(observed),
        "planned_not_run": planned_not_run,
        "weak_tests": weak,
        "dut_rails": dut_rails,
        "rail_gaps": rail_gaps,
        "observed": {
            k: {"samples": v["samples"], "pass": v["pass"], "fail": v["fail"], "run_ids": sorted(list(v["run_ids"]))}
            for k, v in observed.items()
        },
    }


def _coverage_summary_md(bench_id: str, plan_name: str, cov: Dict[str, Any], now: str) -> str:
    lines = []
    lines.append("## Coverage Summary\n")
    lines.append(f"- Time: `{now}`")
    lines.append(f"- Bench: `{bench_id}`")
    lines.append(f"- Plan: `{plan_name}`\n")
    lines.append(f"- Planned tests: `{cov.get('planned_test_count')}`")
    lines.append(f"- Observed tests (in window): `{cov.get('observed_test_count')}`\n")

    pn = cov.get("planned_not_run") or []
    if pn:
        lines.append(f"### Planned but never executed ({len(pn)})")
        for n in pn[:30]:
            lines.append(f"- {n}")
        lines.append("")
    else:
        lines.append("✅ All planned tests have at least one execution in the window.\n")

    weak = cov.get("weak_tests") or []
    if weak:
        lines.append(f"### Weak evidence tests ({len(weak)})")
        for w in weak[:30]:
            lines.append(f"- {w.get('test_name')} (samples={w.get('samples')}, pass={w.get('pass')}, fail={w.get('fail')})")
        lines.append("")

    rg = cov.get("rail_gaps") or []
    if rg:
        lines.append(f"### Rail coverage gaps ({len(rg)})")
        for r in rg[:50]:
            lines.append(f"- {r}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _propose_tests_for_rail_gaps(plan_json: Dict[str, Any], rail_gaps: List[str]) -> List[Dict[str, Any]]:
    """
    MVP deterministic proposal: for each missing DUT rail, add a simple monitoring test.
    """
    proposed: List[Dict[str, Any]] = []
    for r in rail_gaps[:50]:
        proposed.append(
            {
                "name": f"COV_{r}_MONITOR",
                "description": f"Coverage: verify {r} is present and within nominal range.",
                "required_instruments": ["dmm"],
                "measurements": [
                    {"type": "voltage", "rail": r, "units": "V", "limit_low": None, "limit_high": None}
                ],
                "metadata": {"origin": "coverage_gap", "coverage_dim": "rail", "rail": r},
            }
        )
    return proposed


async def compute_and_store_coverage_and_proposal(state: Dict[str, Any], supabase) -> Dict[str, Any]:
    """
    WF8 entrypoint:
    - Resolve active plan by user_id + test_plan_name
    - Compute coverage artifacts from validation_run_facts
    - If gaps exist (planned_not_run or rail_gaps), propose extra tests.
      (MVP: deterministic rail tests + optional LLM summary)
    - Writes proposal artifacts. No DB mutation.
    """
    now = _utc_iso()

    workflow_id = state.get("workflow_id")
    user_id = state.get("user_id")
    bench_id = state.get("bench_id")
    test_plan_name = (state.get("test_plan_name") or "").strip()

    if not workflow_id or not user_id or not bench_id or not test_plan_name:
        state["status"] = "❌ WF8 missing workflow_id/user_id/bench_id/test_plan_name"
        return state

    plan_row = load_active_plan_by_name(supabase, str(user_id), test_plan_name)
    if not plan_row:
        msg = f"❌ No active test plan found for user_id={user_id} name='{test_plan_name}'"
        save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "coverage_no_action.md",
                                      f"## Coverage Proposal\n\n- Time: `{now}`\n\n{msg}\n")
        state["status"] = msg
        return state

    plan_id = plan_row.get("id")
    current_plan = plan_row.get("plan_json") if isinstance(plan_row.get("plan_json"), dict) else {}

    since_iso = _since_ts(WINDOW_DAYS)
    facts = _load_run_facts(supabase, str(bench_id), str(plan_id), since_iso)

    cov = _build_coverage(current_plan, facts)

    # Write coverage artifacts
    save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "coverage_map.json", _json(cov))
    gaps = {
        "planned_not_run": cov.get("planned_not_run") or [],
        "rail_gaps": cov.get("rail_gaps") or [],
        "weak_tests": cov.get("weak_tests") or [],
        "window_days": WINDOW_DAYS,
    }
    save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "coverage_gaps.json", _json(gaps))
    save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "coverage_summary.md",
                                  _coverage_summary_md(str(bench_id), test_plan_name, cov, now))

    # Decide whether to propose changes
    has_gap = bool(gaps["planned_not_run"] or gaps["rail_gaps"])
    if not has_gap:
        save_text_artifact_and_record(
            workflow_id,
            "Validation Coverage Proposal Agent",
            "validation",
            "coverage_no_action.md",
            f"## Coverage Proposal\n\n- Time: `{now}`\n- Bench: `{bench_id}`\n- Plan: `{test_plan_name}`\n\n✅ No action: no coverage gaps detected.\n",
        )
        state["status"] = "✅ WF8 completed (no gaps)"
        state["coverage_proposed"] = False
        return state

    # Build proposed plan (MVP deterministic)
    proposed_plan = json.loads(_json(current_plan))  # deep copy
    proposed_tests = _plan_tests(proposed_plan)
    existing_names = {(_test_name(t)).lower() for t in proposed_tests}

    added = []
    # Rail gap tests
    for t in _propose_tests_for_rail_gaps(current_plan, gaps["rail_gaps"]):
        if _test_name(t).lower() not in existing_names:
            proposed_tests.append(t)
            existing_names.add(_test_name(t).lower())
            added.append(_test_name(t))

    proposed_plan["tests"] = proposed_tests

    # Optional LLM rationale (nice for demo, but not required)
    rationale = (
        "### Rationale\n\n"
        "This proposal adds coverage tests to close detected gaps (rails and/or unexecuted planned tests).\n"
        f"- Added tests: {len(added)}\n"
    )

    # Write proposal artifacts
    old_txt = _json(current_plan)
    new_txt = _json(proposed_plan)
    save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "proposed_test_plan_from_coverage.json", new_txt)
    save_text_artifact_and_record(
        workflow_id,
        "Validation Coverage Proposal Agent",
        "validation",
        "coverage_plan_diff.md",
        "```diff\n" + _diff_text(old_txt, new_txt) + "\n```",
    )
    save_text_artifact_and_record(workflow_id, "Validation Coverage Proposal Agent", "validation", "coverage_plan_rationale.md", rationale)

    # State for WF9 Apply
    state["status"] = "✅ WF8 proposal generated"
    state["coverage_proposed"] = True
    state["proposal_kind"] = "coverage"
    state["proposed_test_plan"] = proposed_plan
    return state
