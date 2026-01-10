import json
import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _norm_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _fmt_constraint(cons: Dict[str, Any]) -> str:
    parts: List[str] = []
    if not isinstance(cons, dict):
        return ""

    vmin = cons.get("vmin")
    vmax = cons.get("vmax")
    imin = cons.get("imin")
    imax = cons.get("imax")

    if vmin is not None:
        parts.append(f"Vmin {vmin}")
    if vmax is not None:
        parts.append(f"Vmax {vmax}")
    if imin is not None:
        parts.append(f"Imin {imin}")
    if imax is not None:
        parts.append(f"Imax {imax}")

    return ", ".join(parts)


def run_agent(state: dict) -> dict:
    """
    Phase-1 Wiring Instructions Agent

    Inputs (state):
      - workflow_id (required)
      - connectivity_intent (preferred)
        OR state["connectivity_intent"] absent: tries state["test_plan"] to reconstruct (not ideal)

    Output artifacts:
      - validation/wiring_instructions.md

    State outputs:
      - state["wiring_instructions_md"] = str
    """
    workflow_id = state.get("workflow_id")
    intent = state.get("connectivity_intent") or {}

    if not workflow_id:
        state["status"] = "❌ Missing workflow_id"
        return state

    if not intent or not isinstance(intent, dict) or not intent.get("nets"):
        state["status"] = "❌ Missing connectivity_intent for wiring instructions"
        return state

    agent_name = "Validation Wiring Instructions Agent"

    dut = intent.get("dut") or {}
    dut_name = _norm_str(dut.get("name")) or "DUT"
    ts = _now_iso()

    lines: List[str] = []
    lines.append(f"# Wiring Instructions — {dut_name}")
    lines.append("")
    lines.append(f"- Generated: {ts}")
    lines.append(f"- Workflow ID: `{workflow_id}`")
    lines.append("")
    lines.append("## Core principle")
    lines.append("Lab engineer wires physical connections using this document. No ChipLoop login required.")
    lines.append("")
    lines.append("## Safety first")
    lines.append("- Confirm DUT absolute maximum ratings before applying power.")
    lines.append("- Start with conservative current limits; ramp voltage slowly.")
    lines.append("- Ensure common ground between DUT and all instruments.")
    lines.append("")

    lines.append("## Required connections (by net)")
    lines.append("")

    nets = intent.get("nets") or []
    # stable output: grounds first, then others
    nets_sorted = sorted(
        nets,
        key=lambda n: (0 if str(n.get("type")).lower() == "ground" else 1, str(n.get("net") or ""))
    )

    for n in nets_sorted:
        if not isinstance(n, dict):
            continue
        net = _norm_str(n.get("net"))
        ntype = _norm_str(n.get("type")).lower()
        req = n.get("required_instruments") or []
        cons = n.get("constraints") or {}
        notes = _norm_str(n.get("notes"))

        if not net:
            continue

        if ntype == "ground":
            lines.append(f"### {net} (Ground)")
            lines.append("- Connect DUT ground to a common ground point shared by PSU/DMM/Scope.")
            lines.append("- Use short, low-impedance ground leads where possible.")
            lines.append("")
            continue

        lines.append(f"### {net}")
        if req:
            lines.append(f"- Instruments required: **{', '.join([str(x) for x in req])}**")
        c = _fmt_constraint(cons)
        if c:
            lines.append(f"- Constraints: **{c}**")
        if notes:
            lines.append(f"- Notes: {notes}")

        # Human-friendly wiring suggestions per instrument type
        req_set = {str(x).strip().lower() for x in req}
        if "psu" in req_set or "smu" in req_set:
            lines.append(f"- PSU/SMU → `{net}` (apply voltage/current limits per constraints)")
        if "dmm" in req_set:
            lines.append(f"- DMM sense → `{net}` (measure DC level / stability)")
        if "scope" in req_set:
            lines.append(f"- Scope CH1 probe tip → `{net}` (observe ripple/noise); Scope GND clip → common GND")

        lines.append("")

    lines.append("## Setup completion checklist")
    lines.append("- [ ] All nets connected per above")
    lines.append("- [ ] Common ground verified")
    lines.append("- [ ] PSU current limit set conservatively")
    lines.append("- [ ] No shorts detected (optional DMM continuity check)")
    lines.append("")
    lines.append("When complete: notify the customer “Setup complete” and share any bench labels / photos if requested.")
    lines.append("")

    md = "\n".join(lines)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name=agent_name,
        subdir="validation",
        filename="wiring_instructions.md",
        content=md,
    )

    state["wiring_instructions_md"] = md
    state["status"] = "✅ Wiring instructions generated"
    return state
