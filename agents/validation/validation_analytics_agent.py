# backend/agents/validation/validation_analytics_agent.py

import json
import math
import datetime
from typing import Any, Dict, Optional, Tuple, List

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
        # Strip common unit suffixes (very light touch)
        for suf in ["V", "A", "mV", "uV", "nV", "Hz", "kHz", "MHz", "GHz", "Ohm", "Ω", "s", "ms", "us", "ns"]:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                break
        try:
            return float(s)
        except Exception:
            return None
    if isinstance(x, dict):
        # common shapes: {"value": 1.23}, {"val": 1.23}, {"reading": 1.23}
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
    """
    Returns {test_name: test_result_obj}
    Accepts multiple shapes; best-effort.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for t in (results or {}).get("tests", []) or []:
        name = t.get("name") or t.get("test_name") or t.get("id") or "unnamed_test"
        out[str(name)] = t
    return out


def _extract_signal_value(test_result: Dict[str, Any], signal: str) -> Optional[float]:
    """
    Attempts to extract a measured value for `signal` from a test result object.
    Supports:
      - test_result["captures"][signal] = number|str|dict
      - test_result["measurements"] = [{"signal":"VOUT","value":1.2}, ...]
      - test_result["readings"][signal] = ...
    """
    if not test_result:
        return None

    # 1) captures dict
    caps = test_result.get("captures")
    if isinstance(caps, dict):
        if signal in caps:
            return _to_float(caps.get(signal))
        # also try case-insensitive match
        for k, v in caps.items():
            if str(k).strip().lower() == str(signal).strip().lower():
                return _to_float(v)

    # 2) readings dict
    readings = test_result.get("readings")
    if isinstance(readings, dict):
        if signal in readings:
            return _to_float(readings.get(signal))
        for k, v in readings.items():
            if str(k).strip().lower() == str(signal).strip().lower():
                return _to_float(v)

    # 3) measurements list
    meas_list = test_result.get("measurements")
    if isinstance(meas_list, list):
        for m in meas_list:
            if not isinstance(m, dict):
                continue
            s = m.get("signal") or m.get("name")
            if s and str(s).strip().lower() == str(signal).strip().lower():
                # common keys for value
                for k in ["value", "val", "reading", "measured", "result"]:
                    if k in m:
                        return _to_float(m.get(k))

    return None


def run_agent(state: dict) -> dict:
    """
    Step 2E: Evaluate captured values against limits from validation/test_plan.json.
    Produces:
      - validation/analytics.json
      - validation/analytics_summary.md
    """
    workflow_id = state.get("workflow_id")
    plan = state.get("scoped_test_plan") or state.get("test_plan") or state.get("validation_test_plan") or {}

    results = state.get("validation_results") or {}

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    if not plan or not isinstance(plan, dict) or not plan.get("tests"):
        state["status"] = "❌ Missing test_plan in state (expected state['test_plan'])"
        return state

    if not results or not isinstance(results, dict) or not results.get("tests"):
        state["status"] = "❌ Missing validation_results in state (expected state['validation_results'])"
        return state

    results_by_test = _index_results_by_test(results)

    analytics: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "dut": plan.get("dut") or {},
        "summary": {
            "tests_total": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "checks_total": 0,
            "checks_passed": 0,
            "checks_failed": 0,
            "checks_missing": 0,
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
            status = "missing"
            ok = False

            if measured_val is not None:
                ok = _within_limit(measured_val, mn, mx)
                status = "pass" if ok else "fail"
            else:
                test_pass = False  # missing measurement should fail for MVP

            if status == "fail":
                test_pass = False

            checks.append(
                {
                    "signal": signal,
                    "method": method,
                    "units": units,
                    "limit": {"min": mn, "max": mx},
                    "measured": measured_val,
                    "status": status,
                }
            )

            analytics["summary"]["checks_total"] += 1
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

    # Artifact: analytics.json
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        rel_path="validation/analytics.json",
        content=json.dumps(analytics, indent=2),
        content_type="application/json",
    )

    # Artifact: analytics_summary.md
    s = analytics["summary"]
    lines = []
    lines.append(f"# Validation Analytics Summary\n")
    lines.append(f"- Workflow: `{workflow_id}`")
    lines.append(f"- Time (UTC): `{analytics['timestamp']}`\n")
    lines.append(f"## Overall\n")
    lines.append(f"- Tests: {s['tests_passed']}/{s['tests_total']} passed")
    lines.append(f"- Checks: {s['checks_passed']}/{s['checks_total']} passed")
    lines.append(f"- Failed checks: {s['checks_failed']}")
    lines.append(f"- Missing checks: {s['checks_missing']}\n")
    lines.append("## Test Results\n")
    for t in analytics["tests"]:
        lines.append(f"### {'✅' if t['pass'] else '❌'} {t['name']}")
        for c in t["checks"]:
            lim = c["limit"]
            lim_str = f"[{lim.get('min')}, {lim.get('max')}]"
            lines.append(
                f"- `{c['signal']}` ({c.get('method')}) = `{c.get('measured')}` {c.get('units') or ''} "
                f"limit={lim_str} → **{c['status']}**"
            )
        lines.append("")

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        rel_path="validation/analytics_summary.md",
        content="\n".join(lines),
        content_type="text/markdown",
    )

    state["validation_analytics"] = analytics
    state["status"] = "✅ Validation analytics computed (limits + pass/fail)"
    return state
