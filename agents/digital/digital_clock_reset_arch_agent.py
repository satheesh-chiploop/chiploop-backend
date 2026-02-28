import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

client_openai = OpenAI() if OpenAI else None


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _find_json_in_state_or_workflow(state: dict, candidates: List[str], workflow_dir: str) -> Optional[str]:
    # 1) direct state keys
    for k in candidates:
        v = state.get(k)
        if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
            return v

    # 2) scan workflow_dir
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.endswith(".json") and ("spec" in fn.lower() or "digital" in fn.lower()):
                return os.path.join(root, fn)
    return None


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _default_clock_reset_arch(spec: Dict[str, Any]) -> Dict[str, Any]:
    # Try to extract clocks/resets if present in spec; otherwise produce a conservative default.
    clocks = spec.get("clocks") or spec.get("clocking") or []
    resets = spec.get("resets") or spec.get("reset") or []

    # Normalize (best-effort)
    norm_clks = []
    for c in clocks if isinstance(clocks, list) else []:
        if isinstance(c, dict):
            norm_clks.append({
                "name": c.get("name", "clk"),
                "frequency_hz": c.get("frequency_hz"),
                "domain": c.get("domain", c.get("name", "clk_domain")),
            })
        elif isinstance(c, str):
            norm_clks.append({"name": c, "frequency_hz": None, "domain": f"{c}_domain"})

    norm_resets = []
    for r in resets if isinstance(resets, list) else []:
        if isinstance(r, dict):
            norm_resets.append({
                "name": r.get("name", "rst_n"),
                "active_level": r.get("active_level", "low"),
                "type": r.get("type", "async_assert_sync_deassert"),
                "domain": r.get("domain"),
            })
        elif isinstance(r, str):
            norm_resets.append({"name": r, "active_level": "low", "type": "async_assert_sync_deassert", "domain": None})

    if not norm_clks:
        norm_clks = [{"name": "clk", "frequency_hz": None, "domain": "clk_domain"}]
    if not norm_resets:
        norm_resets = [{"name": "rst_n", "active_level": "low", "type": "async_assert_sync_deassert", "domain": "clk_domain"}]

    domains = sorted({c["domain"] for c in norm_clks if c.get("domain")})
    domain_resets = []
    for d in domains:
        # Attach the first reset that matches the domain if any; else rst_n
        rmatch = next((r for r in norm_resets if r.get("domain") == d), None)
        domain_resets.append({"domain": d, "reset": (rmatch or norm_resets[0])["name"]})

    return {
        "type": "clock_reset_architecture_intent",
        "version": "1.0",
        "domains": [{"name": d} for d in domains] or [{"name": "clk_domain"}],
        "clocks": norm_clks,
        "resets": norm_resets,
        "domain_reset_map": domain_resets,
        "cdc_intent": {
            "allowed_crossings": [
                {
                    "from_domain": "clk_domain",
                    "to_domain": "clk_domain",
                    "policy": "same_domain_ok"
                }
            ],
            "required_synchronizers": [
                {
                    "kind": "single_bit",
                    "recommended": "2_flop_sync",
                    "notes": "For control/status single-bit crossings."
                },
                {
                    "kind": "bus",
                    "recommended": "async_fifo_or_handshake",
                    "notes": "For multi-bit data signals."
                }
            ],
            "naming_assumptions": [
                "Clock signals often contain 'clk' and resets contain 'rst'/'reset'.",
                "If multiple clocks exist, each sequential always block should map to one clock domain."
            ]
        }
    }

def _extract_ports(spec: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Returns dict with keys: inputs, outputs
    Best-effort based on spec schema.
    """
    inputs: List[str] = []
    outputs: List[str] = []

    ports = spec.get("ports") or spec.get("io") or spec.get("interfaces") or []
    if isinstance(ports, list):
        for p in ports:
            if not isinstance(p, dict):
                continue
            name = p.get("name") or p.get("port") or p.get("signal")
            direction = (p.get("direction") or p.get("dir") or "").lower()
            if not name:
                continue
            if direction in ("in", "input"):
                inputs.append(str(name))
            elif direction in ("out", "output"):
                outputs.append(str(name))
            elif direction in ("inout",):
                # conservative: treat as both so constraints apply
                inputs.append(str(name))
                outputs.append(str(name))

    return {"inputs": sorted(set(inputs)), "outputs": sorted(set(outputs))}


def _pick_primary_clock(arch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Picks a primary clock from arch['clocks'].
    Returns dict with name and frequency_hz (may be None).
    """
    clks = arch.get("clocks") or []
    if isinstance(clks, list) and clks:
        c0 = clks[0]
        if isinstance(c0, dict):
            return {
                "name": c0.get("name") or "clk",
                "frequency_hz": c0.get("frequency_hz"),
            }
        if isinstance(c0, str):
            return {"name": c0, "frequency_hz": None}
    return {"name": "clk", "frequency_hz": None}


def _clock_period_ns(freq_hz: Any) -> float:
    """
    Converts frequency_hz to period_ns; defaults to 10ns if unknown.
    """
    try:
        f = float(freq_hz)
        if f > 0:
            return 1e9 / f
    except Exception:
        pass
    return 10.0  # safe default: 100MHz

def _maybe_llm_refine(spec: Dict[str, Any], arch: Dict[str, Any], log_path: str) -> Dict[str, Any]:
    if not client_openai:
        return arch

    try:
        prompt = (
            "You are a senior SoC clock/reset architect.\n"
            "Refine this clock/reset architecture intent to be industry-standard and CDC-aware.\n"
            "Constraints:\n"
            "- Do NOT invent new functional behavior.\n"
            "- Keep it implementation-agnostic (intent only).\n"
            "- Output valid JSON only.\n\n"
            f"INPUT_SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"DRAFT_ARCH_JSON:\n{json.dumps(arch, indent=2)}\n"
        )
        resp = client_openai.chat.completions.create(
            model=os.getenv("DIGITAL_CLOCK_RESET_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Return JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        txt = resp.choices[0].message.content.strip()
        return json.loads(txt)
    except Exception as e:
        _log(log_path, f"LLM refine skipped/failed: {e}")
        return arch


def run_agent(state: dict) -> dict:
    agent_name = "Clock & Reset Architecture Agent"
    print("\n🕒 Running Clock & Reset Architecture Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "digital_clock_reset_arch_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Clock & Reset Architecture Agent Log\n")

    spec_path = _find_json_in_state_or_workflow(
        state,
        candidates=["spec_json", "digital_spec_json_path", "digital_spec_path", "spec_json_path"],
        workflow_dir=workflow_dir,
    )

    if not spec_path and isinstance(state.get("spec_json"), dict):
        spec = state["spec_json"]
        _log(log_path, "Loaded spec JSON from state['spec_json'] (dict).")
    elif spec_path:
        spec = _load_json(spec_path)
        _log(log_path, f"Loaded spec JSON from: {spec_path}")
    else:
        _log(log_path, "ERROR: Could not locate digital spec JSON.")
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_clock_reset_arch_agent.log",
                                      open(log_path, "r", encoding="utf-8").read())
        state["digital_clock_reset_arch_error"] = "missing_spec_json"
        return state

    arch = _default_clock_reset_arch(spec)
    arch = _maybe_llm_refine(spec, arch, log_path)

        # ---------- SDC generation (50% IO delay rule) ----------
    ports = _extract_ports(spec)
    clk = _pick_primary_clock(arch)
    clk_name = str(clk.get("name") or "clk")
    period_ns = _clock_period_ns(clk.get("frequency_hz"))

    # Resets: exclude them from IO timing by default
    reset_names = []
    for r in (arch.get("resets") or []):
        if isinstance(r, dict) and r.get("name"):
            reset_names.append(str(r["name"]))
        elif isinstance(r, str):
            reset_names.append(r)
    reset_names = sorted(set(reset_names))

    io_delay_ns = 0.5 * period_ns

    # Use robust collection ops. If we can't extract ports reliably, fallback to all_inputs/all_outputs.
    sdc_lines = []
    sdc_lines.append(f"# Auto-generated by {agent_name}")
    sdc_lines.append(f"# Clock: {clk_name} period {period_ns:.3f} ns")
    sdc_lines.append(f"# Default IO delay rule: 50% of clock period ({io_delay_ns:.3f} ns)")
    sdc_lines.append("")
    sdc_lines.append(f"create_clock -name {clk_name} -period {period_ns:.3f} [get_ports {{{clk_name}}}]")
    sdc_lines.append("")

    # Collections
    sdc_lines.append(f"set __CLK_PORTS [get_ports {{{clk_name}}}]")
    if reset_names:
        sdc_lines.append(f"set __RST_PORTS [get_ports {{{' '.join(reset_names)}}}]")
    else:
        sdc_lines.append("set __RST_PORTS [list]")

    sdc_lines.append("set __IN_PORTS  [remove_from_collection [all_inputs]  [add_to_collection $__CLK_PORTS $__RST_PORTS]]")
    sdc_lines.append("set __OUT_PORTS [all_outputs]")
    sdc_lines.append("")
    sdc_lines.append(f"set_input_delay  {io_delay_ns:.3f} -clock {clk_name} $__IN_PORTS")
    sdc_lines.append(f"set_output_delay {io_delay_ns:.3f} -clock {clk_name} $__OUT_PORTS")
    sdc_lines.append("")
    sdc_lines.append(f"set_clock_uncertainty {0.05*period_ns:.3f} [get_clocks {clk_name}]")
    sdc_lines.append("")

    sdc_text = "\n".join(sdc_lines)

    # Upload SDC as a first-class artifact
    save_text_artifact_and_record(workflow_id, agent_name, "digital/constraints", "top.sdc", sdc_text)

    # Put pointer into state for downstream (synth/sta)
    state["sdc_path"] = os.path.join(workflow_dir, "digital", "constraints", "top.sdc")

    arch_json = json.dumps(arch, indent=2)

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "clock_reset_arch_intent.json", arch_json)
    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_clock_reset_arch_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["clock_reset_arch_path"] = os.path.join(workflow_dir, "digital", "clock_reset_arch_intent.json")
    return state
