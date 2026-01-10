import json
import datetime
import re
from typing import Any, Dict, List, Optional, Set

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
        # strip common unit suffixes (best-effort; no heavy parsing)
        for suf in ["mV", "uV", "nV", "V", "mA", "uA", "nA", "A", "Ohm", "Ω"]:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                break
        return float(s)
    except Exception:
        return None


# -----------------------------
# Net extraction helpers
# -----------------------------

# "Sentence-like" hints: verbs + spaces. We do NOT want to treat such strings as net names.
_VERB_HINT_RE = re.compile(r"\b(connect|measure|monitor|probe|attach|sense|tie|hook|clip)\b", re.IGNORECASE)

# Token candidates inside free-form text:
# - allow VIN/VOUT/GND/EN and common net-like patterns (VDD_*, AVDD*, etc.)
# - plus any ALLCAPS/underscore-ish tokens that look like nets
# Keep this permissive but guarded by further validation.
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,31}\b")


def _looks_like_instruction(s: str) -> bool:
    if not s:
        return False
    # Any longer multi-word string is likely instruction text
    if " " in s and (len(s) > 24 or _VERB_HINT_RE.search(s)):
        return True
    return False


def _looks_like_net_token(tok: str) -> bool:
    """
    Decide whether a token is plausibly a net/signal name.
    Avoids hardcoding per DUT; uses naming heuristics.
    """
    if not tok:
        return False
    t = tok.strip()
    if not t or " " in t:
        return False

    # Filter obvious non-nets (common instruction words)
    low = t.lower()
    if low in {
        "connect", "measure", "monitor", "probe", "attach", "sense", "tie",
        "to", "and", "or", "both", "with", "without", "using",
        "dmm", "psu", "smu", "scope", "gndclip", "groundclip",
        "channel", "ch", "tip", "clip", "common", "required",
        "apply", "limits", "per", "constraints", "dc", "ac", "level", "stability",
        "ripple", "noise"
    }:
        return False

    # Accept very common explicit nets (minimal “hardcoding” to reduce false negatives)
    if t.upper() in {"VIN", "VOUT", "GND", "EN"}:
        return True

    # Accept power-rail-ish prefixes
    up = t.upper()
    if up.startswith(("VDD", "VSS", "AVDD", "DVDD", "VCC", "VEE", "VBAT", "VREF", "IO", "GPIO")):
        return True

    # Generic heuristic: many net names are ALLCAPS/underscored or short alpha-num
    # Example: "FB", "PGOOD", "SDA", "SCL", "SW", "COMP", "RT", "ILIM"
    if (t.isupper() and 2 <= len(t) <= 12):
        return True

    # Underscore net pattern (VDD_CPU, VOUT_SENSE, etc.)
    if "_" in t and len(t) <= 32:
        return True

    return False


def _extract_net_tokens_from_text(s: str) -> List[str]:
    """
    Extract plausible net tokens from instruction text.
    """
    if not s:
        return []
    tokens = _TOKEN_RE.findall(s)
    out: List[str] = []
    seen: Set[str] = set()
    for tok in tokens:
        if _looks_like_net_token(tok):
            key = tok.strip()
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _extract_candidate_nets_from_plan(plan: Dict[str, Any]) -> Set[str]:
    """
    Try to find nets/signals from common plan shapes:
      - test.setup.rails
      - test.setup.connections (dict or string)
      - test.measurements[].signal/name
    Fix: if a "connection" is sentence-like, parse net tokens instead of using it directly.
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
                        # rails should be single net tokens; still guard a bit
                        if _looks_like_instruction(rn):
                            for tok in _extract_net_tokens_from_text(rn):
                                nets.add(tok)
                        else:
                            nets.add(rn)
                else:
                    rn = _norm_str(r)
                    if rn:
                        if _looks_like_instruction(rn):
                            for tok in _extract_net_tokens_from_text(rn):
                                nets.add(tok)
                        else:
                            nets.add(rn)

            # connections
            for c in _as_list(setup.get("connections")):
                if isinstance(c, dict):
                    cn = _norm_str(c.get("net") or c.get("signal") or c.get("name"))
                    if not cn:
                        continue
                    if _looks_like_instruction(cn):
                        for tok in _extract_net_tokens_from_text(cn):
                            nets.add(tok)
                    else:
                        nets.add(cn)
                else:
                    cn = _norm_str(c)
                    if not cn:
                        continue
                    if _looks_like_instruction(cn):
                        for tok in _extract_net_tokens_from_text(cn):
                            nets.add(tok)
                    else:
                        # If it has spaces but wasn't flagged as instruction, still avoid adding raw sentence.
                        if " " in cn:
                            for tok in _extract_net_tokens_from_text(cn):
                                nets.add(tok)
                        else:
                            nets.add(cn)

        # measurements
        for m in _as_list(t.get("measurements")):
            if not isinstance(m, dict):
                continue
            sig = _norm_str(m.get("signal") or m.get("name"))
            if not sig:
                continue
            if _looks_like_instruction(sig):
                for tok in _extract_net_tokens_from_text(sig):
                    nets.add(tok)
            else:
                nets.add(sig)

    # If nothing found, keep a safe minimal default so Phase-1 still produces artifacts
    if not nets:
        nets.update({"VIN", "VOUT", "GND"})

    return nets


def _extract_constraints_for_net(plan: Dict[str, Any], net: str) -> Dict[str, Any]:
    """
    Extract basic constraints from measurement limits if available:
      - vmin/vmax from voltage measurements
      - imin/imax from current measurements
    """
    vmax: Optional[float] = None
    vmin: Optional[float] = None
    imax: Optional[float] = None
    imin: Optional[float] = None

    net_l = net.strip().lower()

    for t in _as_list(plan.get("tests")):
        if not isinstance(t, dict):
            continue
        for m in _as_list(t.get("measurements")):
            if not isinstance(m, dict):
                continue
            sig = _norm_str(m.get("signal") or m.get("name"))
            if not sig:
                continue

            # Accept direct match OR token-in-sentence match
            sig_tokens = {x.lower() for x in _extract_net_tokens_from_text(sig)} if _looks_like_instruction(sig) else {sig.strip().lower()}
            if net_l not in sig_tokens:
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


def _test_mentions_net(t: Dict[str, Any], net: str) -> bool:
    """
    Determine whether a test references a given net.
    Handles:
      - setup.connections entries as strings/dicts (including sentence-like strings)
      - measurements signal/name (including sentence-like strings)
      - setup.rails
    """
    net_l = net.strip().lower()

    setup = t.get("setup") or {}
    if isinstance(setup, dict):
        # rails
        for r in _as_list(setup.get("rails")):
            rn = ""
            if isinstance(r, dict):
                rn = _norm_str(r.get("name") or r.get("rail") or r.get("net"))
            else:
                rn = _norm_str(r)

            if not rn:
                continue
            tokens = {x.lower() for x in _extract_net_tokens_from_text(rn)} if _looks_like_instruction(rn) else {rn.strip().lower()}
            if net_l in tokens:
                return True

        # connections
        for c in _as_list(setup.get("connections")):
            cn = ""
            if isinstance(c, dict):
                cn = _norm_str(c.get("net") or c.get("signal") or c.get("name"))
            else:
                cn = _norm_str(c)
            if not cn:
                continue
            tokens = {x.lower() for x in _extract_net_tokens_from_text(cn)} if _looks_like_instruction(cn) else {cn.strip().lower()}
            if net_l in tokens:
                return True

    # measurements
    for m in _as_list(t.get("measurements")):
        if not isinstance(m, dict):
            continue
        sig = _norm_str(m.get("signal") or m.get("name"))
        if not sig:
            continue
        tokens = {x.lower() for x in _extract_net_tokens_from_text(sig)} if _looks_like_instruction(sig) else {sig.strip().lower()}
        if net_l in tokens:
            return True

    return False


def _required_instruments_for_net(plan: Dict[str, Any], net: str) -> List[str]:
    """
    Infer instruments required for a net:
      - union of test.required_instruments for tests that mention the net
      - fallback to conservative defaults if nothing found
    """
    req: Set[str] = set()

    for t in _as_list(plan.get("tests")):
        if not isinstance(t, dict):
            continue

        if _test_mentions_net(t, net):
            for inst in _as_list(t.get("required_instruments")):
                s = _norm_str(inst).lower()
                if s:
                    req.add(s)

    if not req:
        # conservative defaults: Phase-1 intent should never block wiring doc generation
        req.update({"psu", "dmm"})

    # stable ordering (not device-specific; just keeps output deterministic)
    order = ["psu", "smu", "dmm", "scope", "logic_analyzer", "temp_chamber"]
    return sorted(req, key=lambda x: order.index(x) if x in order else 999)


def run_agent(state: dict) -> dict:
    """
    Phase-1 Connectivity Intent Generator Agent

    Inputs (state):
      - workflow_id (required)
      - test_plan OR validation_test_plan OR scoped_test_plan (required)

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

    nets = sorted(_extract_candidate_nets_from_plan(plan))

    net_specs: List[Dict[str, Any]] = []
    for net in nets:
        if not net:
            continue

        # Treat GND specially
        if net.strip().lower() in ["gnd", "ground", "0v"]:
            net_specs.append(
                {
                    "net": net,
                    "type": "ground",
                    "required_instruments": [],
                    "constraints": {},
                    "notes": "Common ground reference required across all instruments.",
                }
            )
            continue

        net_specs.append(
            {
                "net": net,
                "type": "signal",
                "required_instruments": _required_instruments_for_net(plan, net),
                "constraints": _extract_constraints_for_net(plan, net),
                "notes": "",
            }
        )

    intent: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "timestamp": _now_iso(),
        "dut": {"name": dut_name, "notes": _norm_str(dut.get("notes"))},
        "principle": "Logical connectivity intent only (no physical resource strings).",
        "common_ground_required": True,
        "nets": net_specs,
        "safety_defaults": {
            "vmax_policy": "Do not exceed DUT absolute maximum ratings; apply headroom if uncertain.",
            "imax_policy": "Start with low current limit; ramp carefully.",
        },
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
