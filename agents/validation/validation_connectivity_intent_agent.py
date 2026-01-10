import json
import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _norm_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return float(x)
        s = str(x).strip()
        if not s:
            return None
        # strip common unit suffixes
        for suf in ["V", "A", "mV", "uV", "nV", "Ohm", "Ω"]:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                break
        return float(s)
    except Exception:
        return None


def _extract_candidate_nets_from_plan(plan: Dict[str, Any]) -> Set[str]:
    """
    Try to find 'nets' / signals from common plan shapes:
      - test.setup.connections: list of strings or dicts with net/signal
      - test.measurements: list of dicts with signal/name
      - test.setup.rails: rail names
    """
    nets: Set[str] = set()

    for t in _as_list(plan.get("tests")):
        if not isinstance(t, dict):
            continue

        setup = t.get("setup") or {}
        if isinstance(setup, dict):
            # rails
            for r in _as_list(setup.get("rails")):
                if isinstance(r, dict):
                    rn = _norm_str(r.get("name") or r.get("rail") or r.get("net"))
                    if rn:
                        nets.add(rn)
                else:
                    rn = _norm_str(r)
                    if rn:
                        nets.add(rn)

            # connections
            for c in _as_list(setup.get("connections")):
                if isinstance(c, dict):
                    cn = _norm_str(c.get("net") or c.get("signal") or c.get("name"))
                    if cn:
                        nets.add(cn)
                else:
                    cn = _norm_str(c)
                    if cn:
                        nets.add(cn)

        # measurements
        for m in _as_list(t.get("measurements")):
            if not isinstance(m, dict):
                continue
            sig = _norm_str(m.get("signal") or m.get("name"))
            if sig:
                nets.add(sig)

    # If nothing found, give a safe default so Phase-1 still produces something demo-friendly
    if not nets:
        nets.add("VDD")
        nets.add("GND")
        nets.add("VOUT")

    return nets


def _extract_constraints_for_net(plan: Dict[str, Any], net: str) -> Dict[str, Any]:
    """
    Extract basic constraints from measurement limits if available:
      - vmax from measurement.limit.max when units indicate voltage
      - imin/imax similarly if current appears (rare in your schema right now)
    """
    vmax: Optional[float] = None
    vmin: Optional[float] = None
    imax: Optional[float] = None
    imin: Optional[float] = None

    for t in _as_list(plan.get("tests")):
        if not isinstance(t, dict):
            continue
        for m in _as_list(t.get("measurements")):
            if not isinstance(m, dict):
                continue
            sig = _norm_str(m.get("signal") or m.get("name"))
            if sig.strip().lower() != net.strip().lower():
                continue

            units = _norm_str(m.get("units")).lower()
            lim = m.get("limit") or {}
            if not isinstance(lim, dict):
                lim = {}

            mn = _safe_float(lim.get("min"))
            mx = _safe_float(lim.get("max"))

            if "v" in units:
                if mn is not None:
                    vmin = mn if vmin is None else min(vmin, mn)
                if mx is not None:
                    vmax = mx if vmax is None else max(vmax, mx)

            if "a" in units:
                if mn is not None:
                    imin = mn if imin is None else min(imin, mn)
                if mx is not None:
                    imax = mx if imax is None else max(imax, mx)

    constraints: Dict[str, Any] = {}
    if vmin is not None:
        constraints["vmin"] = vmin
    if vmax is not None:
        constraints["vmax"] = vmax
    if imin is not None:
        constraints["imin"] = imin
    if imax is not None:
        constraints["imax"] = imax

    return constraints


def _required_instruments_for_net(plan: Dict[str, Any], net: str) -> List[str]:
    """
    Infer instruments required for a net:
      - union of required_instruments from any test referencing that net
      - if none found: default to ["psu","dmm"] (safe)
    """
    req: Set[str] = set()

    for t in _as_list(plan.get("tests")):
        if not isinstance(t, dict):
            continue

        touches_net = False

        # setup connections
        setup = t.get("setup") or {}
        if isinstance(setup, dict):
            for c in _as_list(setup.get("connections")):
                if isinstance(c, dict):
                    cn = _norm_str(c.get("net") or c.get("signal") or c.get("name"))
                else:
                    cn = _norm_str(c)
                if cn and cn.strip().lower() == net.strip().lower():
                    touches_net = True
                    break

        # measurements
        if not touches_net:
            for m in _as_list(t.get("measurements")):
                if not isinstance(m, dict):
                    continue
                sig = _norm_str(m.get("signal") or m.get("name"))
                if sig and sig.strip().lower() == net.strip().lower():
                    touches_net = True
                    break

        if touches_net:
            for inst in _as_list(t.get("required_instruments")):
                s = _norm_str(inst).lower()
                if s:
                    req.add(s)

    if not req:
        # demo-friendly conservative defaults
        req.update(["psu", "dmm"])

    # stable ordering
    order = ["psu", "smu", "dmm", "scope", "logic_analyzer", "temp_chamber"]
    out = sorted(req, key=lambda x: order.index(x) if x in order else 999)
    return out


def run_agent(state: dict) -> dict:
    """
    Phase-1 Connectivity Intent Generator Agent

    Inputs (state):
      - workflow_id (required)
      - test_plan OR validation_test_plan (required)
      - optional: goal, org/lab metadata, user constraints

    Output artifacts:
      - validation/connectivity_intent.json

    State outputs:
      - state["connectivity_intent"] = dict
    """
    workflow_id = state.get("workflow_id")
    plan = state.get("test_plan") or state.get("validation_test_plan") or state.get("scoped_test_plan") or {}

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    if not plan or not isinstance(plan, dict) or not plan.get("tests"):
        state["status"] = "❌ Missing test_plan for connectivity intent"
        return state

    agent_name = "Validation Connectivity Intent Agent"

    dut = plan.get("dut") or {}
    dut_name = _norm_str(dut.get("name")) or "DUT"

    nets = sorted(list(_extract_candidate_nets_from_plan(plan)))

    net_specs: List[Dict[str, Any]] = []
    for net in nets:
        if not net:
            continue

        # Treat GND specially
        if net.strip().lower() in ["gnd", "ground", "0v"]:
            net_specs.append({
                "net": net,
                "type": "ground",
                "required_instruments": [],
                "constraints": {},
                "notes": "Common ground reference required across all instruments."
            })
            continue

        net_specs.append({
            "net": net,
            "type": "signal",
            "required_instruments": _required_instruments_for_net(plan, net),
            "constraints": _extract_constraints_for_net(plan, net),
            "notes": ""
        })

    intent: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "timestamp": _now_iso(),
        "dut": {
            "name": dut_name,
            "notes": _norm_str(dut.get("notes")),
        },
        "principle": "Logical connectivity intent only (no physical resource strings).",
        "common_ground_required": True,
        "nets": net_specs,
        "safety_defaults": {
            "vmax_policy": "Do not exceed DUT absolute maximum ratings; apply headroom if uncertain.",
            "imax_policy": "Start with low current limit; ramp carefully."
        }
    }

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="connectivity_intent.json",
        content=json.dumps(intent, indent=2),
    )

    state["connectivity_intent"] = intent
    state["status"] = "✅ Connectivity intent generated"
    return state
