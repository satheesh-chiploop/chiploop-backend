import json
from utils.artifact_utils import save_text_artifact_and_record

# ---------------------------------------------------------------------
# System Loop: Top Assembly Agent
# - Reads system_integration_intent.json from state and generates:
#   * <top>_sim.sv  (ADC behavioral model via variants.sim.module_overrides)
#   * <top>_phys.sv (ADC macro stub via variants.phys.module_overrides)
# - Designed to avoid touching digital_top_assembly_agent.py.
# ---------------------------------------------------------------------


def _parse_endpoint(ep: str):
    """
    "u_inst.port" -> ("u_inst", "port")
    """
    if not ep or "." not in ep:
        return None, None
    inst, port = ep.split(".", 1)
    return inst.strip(), port.strip()


def _wire_name(i: int, src: str, dst: str) -> str:
    # stable-ish wire naming
    src_s = src.replace(".", "_").replace("[", "_").replace("]", "_")
    dst_s = dst.replace(".", "_").replace("[", "_").replace("]", "_")
    return f"w_{i}_{src_s}__{dst_s}"


def _assemble_top(top_module: str, intent: dict, variant: str) -> str:
    """
    Build a simple SystemVerilog top with:
    - logic wires inferred from connections
    - named port maps for connected + tieoff ports
    - module overrides applied per intent['variants'][variant]['module_overrides']
    """
    instances = intent.get("instances", [])
    connections = intent.get("connections", [])
    tieoffs = intent.get("tieoffs", [])

    variants = intent.get("variants", {}) or {}
    module_overrides = (variants.get(variant, {}) or {}).get("module_overrides", {}) or {}

    # Build per-instance port map
    port_map = {inst["name"]: {} for inst in instances if "name" in inst}

    # Apply tieoffs: "u_adc.test_mode" => value
    for t in tieoffs:
        sig = t.get("signal")
        val = t.get("value")
        if not sig or val is None:
            continue
        inst, port = _parse_endpoint(sig)
        if inst in port_map and port:
            port_map[inst][port] = val  # direct literal tieoff

    # Apply connections: create wires and map both endpoints to same wire
    wire_decls = []
    for idx, c in enumerate(connections):
        src = c.get("from")
        dst = c.get("to")
        if not src or not dst:
            continue
        si, sp = _parse_endpoint(src)
        di, dp = _parse_endpoint(dst)
        if not (si and sp and di and dp):
            continue

        w = _wire_name(idx, src, dst)
        wire_decls.append(f"  logic {w};")
        if si in port_map:
            port_map[si][sp] = w
        if di in port_map:
            port_map[di][dp] = w

    lines = []
    lines.append(f"module {top_module} ();")
    lines.append("")

    if wire_decls:
        lines.append("  // Auto-generated interconnect wires")
        lines.extend(wire_decls)
        lines.append("")

    # Instances
    for inst in instances:
        name = inst.get("name")
        module = inst.get("module")
        if not name or not module:
            continue

        # Apply module override for this variant (e.g., u_adc -> adc_behavioral in sim)
        module = module_overrides.get(name, module)

        pm = port_map.get(name, {})
        if pm:
            lines.append(f"  {module} {name} (")
            # stable order: literal tieoffs first? just sort keys
            keys = sorted(pm.keys())
            for k_i, port in enumerate(keys):
                rhs = pm[port]
                comma = "," if k_i < len(keys) - 1 else ""
                lines.append(f"    .{port}({rhs}){comma}")
            lines.append("  );")
        else:
            lines.append(f"  {module} {name} ();")

        lines.append("")

    lines.append("endmodule")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    agent_name = "System Top Assembly Agent"
    workflow_id = state.get("workflow_id")
    top_intent = state.get("system_integration_intent") or {}
    top_cfg = top_intent.get("top", {}) if isinstance(top_intent, dict) else {}

    top_base = (top_cfg.get("base_name") or state.get("soc_top_name") or state.get("top_module") or "soc_top").strip()
    top_sim = (top_cfg.get("sim_module") or f"{top_base}_sim").strip()
    top_phys = (top_cfg.get("phys_module") or f"{top_base}_phys").strip()

    # Generate tops
    sim_code = _assemble_top(top_sim, top_intent, variant="sim")
    phys_code = _assemble_top(top_phys, top_intent, variant="phys")

    # Persist artifacts
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integrate",
        f"{top_sim}.sv",
        sim_code,
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integrate",
        f"{top_phys}.sv",
        phys_code,
    )

    # Update state for downstream consumers
    state["soc_top_sim_module"] = top_sim
    state["soc_top_phys_module"] = top_phys
    state["soc_top_sim_path"] = f"system/integrate/{top_sim}.sv"
    state["soc_top_phys_path"] = f"system/integrate/{top_phys}.sv"

    state["status"] = f"✅ Generated SoC tops: {top_sim}.sv and {top_phys}.sv"
    return state
