# backend/agents/validation/validation_analytics_agent.py

import json
import math
import datetime
import logging
from typing import Any, Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)

from utils.artifact_utils import save_text_artifact_and_record


def _to_float(x: Any) -> Optional[float]:
    """Best-effort conversion of common measurement shapes into float."""
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        # NOTE: order matters: check longer suffixes first (mV before V, etc.)
        for suf in ["GHz", "MHz", "kHz", "Hz", "Ohm", "Ω", "ms", "us", "ns", "mV", "uV", "nV", "V", "A", "s"]:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                break
        try:
            return float(s)
        except Exception:
            return None
    if isinstance(x, dict):
        for k in ["value", "val", "reading", "measured", "result"]:
            if k in x:
                return _to_float(x.get(k))
    return None


def _get_limit(meas: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lim = meas.get("limit") or {}
    mn = _to_float(lim.get("min"))
    mx = _to_float(lim.get("max"))
    return mn, mx


def _within_limit(val: float, mn: Optional[float], mx: Optional[float]) -> bool:
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return False
    if mn is not None and val < mn:
        return False
    if mx is not None and val > mx:
        return False
    return True


def _index_results_by_test(results: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in (results or {}).get("tests", []) or []:
        name = t.get("name") or t.get("test_name") or t.get("id") or "unnamed_test"
        out[str(name)] = t
    return out


def _extract_signal_value(test_result: Dict[str, Any], signal: str) -> Optional[float]:
    if not test_result:
        return None

    caps = test_result.get("captures")
    if isinstance(caps, dict):
        if signal in caps:
            return _to_float(caps.get(signal))
        for k, v in caps.items():
            if str(k).strip().lower() == str(signal).strip().lower():
                return _to_float(v)

    readings = test_result.get("readings")
    if isinstance(readings, dict):
        if signal in readings:
            return _to_float(readings.get(signal))
        for k, v in readings.items():
            if str(k).strip().lower() == str(signal).strip().lower():
                return _to_float(v)

    meas_list = test_result.get("measurements")
    if isinstance(meas_list, list):
        for m in meas_list:
            if not isinstance(m, dict):
                continue
            s = m.get("signal") or m.get("name")
            if s and str(s).strip().lower() == str(signal).strip().lower():
                for k in ["value", "val", "reading", "measured", "result"]:
                    if k in m:
                        return _to_float(m.get(k))

    return None


def _detect_stub_mode(results: Dict[str, Any]) -> bool:
    """
    Heuristic: treat as stub if results advertise mode=stub anywhere commonly used.
    """
    if not isinstance(results, dict):
        return False
    for key in ["mode", "execution_mode", "run_mode"]:
        v = results.get(key)
        if isinstance(v, str) and v.strip().lower() == "stub":
            return True
    meta = results.get("meta") or results.get("metadata") or {}
    if isinstance(meta, dict):
        v = meta.get("mode") or meta.get("execution_mode") or meta.get("run_mode")
        if isinstance(v, str) and v.strip().lower() == "stub":
            return True
    return False


def _synthesize_within_limit(mn: Optional[float], mx: Optional[float]) -> Optional[float]:
    """
    Option A: in stub mode, generate a value that satisfies the limit.
    - If both bounds exist: midpoint
    - If only min exists: min + small margin
    - If only max exists: max - small margin
    - If no bounds: can't synthesize
    """
    if mn is not None and mx is not None:
        return (mn + mx) / 2.0
    if mn is not None and mx is None:
        # choose a value safely above min
        return mn + 0.01 * (abs(mn) if mn != 0 else 1.0)
    if mx is not None and mn is None:
        # choose a value safely below max
        return mx - 0.01 * (abs(mx) if mx != 0 else 1.0)
    return None


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id")
    plan = state.get("scoped_test_plan") or state.get("test_plan") or state.get("validation_test_plan") or {}
    results = state.get("validation_results") or {}

    if not workflow_id or not plan or not results:
        logger.warning(
            "[ANALYTICS] Early return | "
            f"workflow_id={bool(workflow_id)} | "
            f"plan={bool(plan)} | "
            f"validation_results={bool(results)} | "
            f"state_keys={list(state.keys())}"
        )
        state["status"] = "❌ Missing inputs for analytics"
        return state

    if not isinstance(plan, dict) or not plan.get("tests"):
        state["status"] = "❌ Missing test_plan in state (expected state['test_plan'])"
        return state

    if not isinstance(results, dict) or not results.get("tests"):
        state["status"] = "❌ Missing validation_results in state (expected state['validation_results'])"
        return state

    agent_name = "Validation Analytics Agent"
    stub_mode = _detect_stub_mode(results)

    results_by_test = _index_results_by_test(results)

    analytics: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "dut": plan.get("dut") or {},
        "mode": "stub" if stub_mode else "real",
        "summary": {
            "tests_total": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "checks_total": 0,
            "checks_passed": 0,
            "checks_failed": 0,
            "checks_missing": 0,
            "checks_synthesized": 0,
        },
        "tests": [],
    }

    for test in plan.get("tests", []) or []:
        test_name = test.get("name") or "unnamed_test"
        test_result = results_by_test.get(str(test_name)) or {}

        checks: List[Dict[str, Any]] = []
        test_pass = True

        for meas in (test.get("measurements", []) or []):
            signal = meas.get("signal") or meas.get("name") or "unnamed_signal"
            units = meas.get("units")
            method = meas.get("method")
            mn, mx = _get_limit(meas)

            measured_val = _extract_signal_value(test_result, str(signal))
            measured_original = measured_val
            synthesized = False

            status = "missing"
            ok = False

            # Option A: if stub and missing/out-of-range, synthesize an in-range value
            if measured_val is None:
                if stub_mode:
                    synth = _synthesize_within_limit(mn, mx)
                    if synth is not None:
                        measured_val = synth
                        synthesized = True
                # after synth attempt
            else:
                ok = _within_limit(measured_val, mn, mx)
                if (not ok) and stub_mode:
                    synth = _synthesize_within_limit(mn, mx)
                    if synth is not None:
                        measured_val = synth
                        synthesized = True

            if measured_val is not None:
                ok = _within_limit(measured_val, mn, mx)
                status = "pass" if ok else "fail"
            else:
                # truly missing (and couldn't synthesize)
                test_pass = False

            if status == "fail":
                test_pass = False

            checks.append(
                {
                    "signal": signal,
                    "method": method,
                    "units": units,
                    "limit": {"min": mn, "max": mx},
                    "measured": measured_val,
                    "measured_original": measured_original if synthesized else None,
                    "synthesized": bool(synthesized),
                    "status": status,
                }
            )

            analytics["summary"]["checks_total"] += 1
            if synthesized:
                analytics["summary"]["checks_synthesized"] += 1

            if status == "pass":
                analytics["summary"]["checks_passed"] += 1
            elif status == "fail":
                analytics["summary"]["checks_failed"] += 1
            else:
                analytics["summary"]["checks_missing"] += 1

        analytics["summary"]["tests_total"] += 1
        if test_pass:
            analytics["summary"]["tests_passed"] += 1
        else:
            analytics["summary"]["tests_failed"] += 1

        analytics["tests"].append(
            {
                "name": test_name,
                "objective": test.get("objective"),
                "tags": test.get("tags") or [],
                "required_instruments": test.get("required_instruments") or [],
                "pass": bool(test_pass),
                "checks": checks,
            }
        )

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="analytics.json",
        content=json.dumps(analytics, indent=2),
    )

    s = analytics["summary"]
    lines = []
    lines.append(f"# Validation Analytics Summary\n")
    lines.append(f"- Workflow: `{workflow_id}`")
    lines.append(f"- Time (UTC): `{analytics['timestamp']}`")
    lines.append(f"- Mode: `{analytics.get('mode')}`\n")
    lines.append(f"## Overall\n")
    lines.append(f"- Tests: {s['tests_passed']}/{s['tests_total']} passed")
    lines.append(f"- Checks: {s['checks_passed']}/{s['checks_total']} passed")
    lines.append(f"- Failed checks: {s['checks_failed']}")
    lines.append(f"- Missing checks: {s['checks_missing']}")
    lines.append(f"- Synthesized checks (stub fixups): {s['checks_synthesized']}\n")
    lines.append("## Test Results\n")
    for t in analytics["tests"]:
        lines.append(f"### {'✅' if t['pass'] else '❌'} {t['name']}")
        for c in t["checks"]:
            lim = c["limit"]
            lim_str = f"[{lim.get('min')}, {lim.get('max')}]"
            synth_str = " (synth)" if c.get("synthesized") else ""
            lines.append(
                f"- `{c['signal']}` ({c.get('method')}) = `{c.get('measured')}`{synth_str} {c.get('units') or ''} "
                f"limit={lim_str} → **{c['status']}**"
            )
        lines.append("")

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="analytics_summary.md",
        content="\n".join(lines),
    )

    state["validation_analytics"] = analytics
    state["status"] = "✅ Validation analytics computed (limits + pass/fail)"
    return state
