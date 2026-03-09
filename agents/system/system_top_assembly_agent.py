import json
import re
from utils.artifact_utils import save_text_artifact_and_record

# ---------------------------------------------------------------------
# System Loop: Top Assembly Agent
# - Reads system_integration_intent.json from state and generates:
#   * <top>_sim.sv
#   * <top>_phys.sv
# - Converts top.* endpoints into real top ports
# - Sanitizes bus endpoints and wire names
# ---------------------------------------------------------------------


def _parse_endpoint(ep: str):
    """
    "u_inst.port" -> ("u_inst", "port")
    "top.DATA[9:0]" -> ("top", "DATA[9:0]")
    """
    if not ep or "." not in ep:
        return None, None
    inst, port = ep.split(".", 1)
    return inst.strip(), port.strip()


def _split_port_and_range(port: str):
    """
    "adc_data[9:0]" -> ("adc_data", "[9:0]")
    "clk" -> ("clk", "")
    """
    if not port:
        return "", ""
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_$]*)(\[[^\]]+\])?$", port.strip())
    if not m:
        cleaned = re.sub(r"[^a-zA-Z0-9_$]", "_", port.strip())
        return cleaned, ""
    return m.group(1), (m.group(2) or "")


def _sanitize_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_$]", "_", name)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "sig"
    if s[0].isdigit():
        s = f"n_{s}"
    return s


def _wire_name(i: int, src: str, dst: str) -> str:
    src_inst, src_port = _parse_endpoint(src)
    dst_inst, dst_port = _parse_endpoint(dst)
    src_base, _ = _split_port_and_range(src_port or "")
    dst_base, _ = _split_port_and_range(dst_port or "")
    return _sanitize_name(f"w_{i}_{src_inst}_{src_base}__{dst_inst}_{dst_base}")


def _merge_range(a: str, b: str) -> str:
    return a or b or ""


def _top_dir_for_endpoint(is_source: bool) -> str:
    # If top is source of connection, it is an input to top-level module.
    # If top is destination of connection, it is an output from top-level module.
    return "input" if is_source else "output"


def _normalize_tieoff_entry(t: dict):
    """
    Support both:
      {"signal":"u_a.port","value":"1'b0"}
      {"instance":"u_a","port":"enable","value":"1'b0"}
    """
    sig = t.get("signal")
    if sig:
        inst, port = _parse_endpoint(sig)
        return inst, port, t.get("value")

    inst = t.get("instance")
    port = t.get("port")
    return inst, port, t.get("value")


def _assemble_top(top_module: str, intent: dict, variant: str) -> str:
    instances = intent.get("instances", [])
    connections = intent.get("connections", [])
    tieoffs = intent.get("tieoffs", [])

    variants = intent.get("variants", {}) or {}
    module_overrides = (variants.get(variant, {}) or {}).get("module_overrides", {}) or {}

    port_map = {inst["name"]: {} for inst in instances if "name" in inst}
    top_ports = {}   # name -> {"dir": "...", "range": "..."}
    wire_meta = {}   # wire_name -> range
    wire_decls = []

    # tieoffs
    for t in tieoffs:
        inst, port, val = _normalize_tieoff_entry(t)
        if not inst or not port or val is None:
            continue
        base, rng = _split_port_and_range(port)
        if inst == "top":
            # top-level tieoff does not make sense; skip
            continue
        if inst in port_map and base:
            port_map[inst][base] = str(val)
    # Track destinations to prevent silent double-drives
    driven_instance_ports = set()
    driven_top_ports = set()

    # connections
    for idx, c in enumerate(connections):
        src = c.get("from")
        dst = c.get("to")
        if not src or not dst:
            continue

        si, sp_raw = _parse_endpoint(src)
        di, dp_raw = _parse_endpoint(dst)
        if not (si and sp_raw and di and dp_raw):
            continue

        sp, sr = _split_port_and_range(sp_raw)
        dp, dr = _split_port_and_range(dp_raw)
        width = _merge_range(sr, dr)

        # Case 1: top -> instance
        if si == "top" and di != "top":
            dst_key = (di, dp)
            if dst_key in driven_instance_ports:
                continue
            driven_instance_ports.add(dst_key)

            top_ports[sp] = {"dir": _top_dir_for_endpoint(True), "range": sr}
            if di in port_map:
                port_map[di][dp] = sp
            continue

        # Case 2: instance -> top
        if si != "top" and di == "top":
            if dp in driven_top_ports:
                continue
            driven_top_ports.add(dp)

            top_ports[dp] = {"dir": _top_dir_for_endpoint(False), "range": dr}
            if si in port_map:
                port_map[si][sp] = dp
            continue

        # Case 3: top -> top, ignore
        if si == "top" and di == "top":
            continue

        # Case 4: instance -> instance
        dst_key = (di, dp)
        if dst_key in driven_instance_ports:
            continue
        driven_instance_ports.add(dst_key)

        w = _wire_name(idx, src, dst)
        wire_meta[w] = width
        if si in port_map:
            port_map[si][sp] = w
        if di in port_map:
            port_map[di][dp] = w
    

    for w, rng in wire_meta.items():
        if rng:
            wire_decls.append(f"  logic {rng} {w};")
        else:
            wire_decls.append(f"  logic {w};")

    # module header
    lines = []
    port_items = []
    for pname in sorted(top_ports.keys()):
        meta = top_ports[pname]
        d = meta["dir"]
        r = meta["range"]
        if r:
            port_items.append(f"  {d} logic {r} {pname}")
        else:
            port_items.append(f"  {d} logic {pname}")

    if port_items:
        lines.append(f"module {top_module} (")
        for i, item in enumerate(port_items):
            comma = "," if i < len(port_items) - 1 else ""
            lines.append(f"{item}{comma}")
        lines.append(");")
    else:
        lines.append(f"module {top_module} ();")
    lines.append("")

    if wire_decls:
        lines.append("  // Auto-generated interconnect wires")
        lines.extend(wire_decls)
        lines.append("")


    # instances (sorted for deterministic output)
    for inst in sorted(instances, key=lambda x: x.get("name", "")):
        name = inst.get("name")
        module = inst.get("module")
        if not name or not module:
            continue

        module = module_overrides.get(name, module)
        pm = port_map.get(name, {})

        if pm:
            lines.append(f"  {module} {name} (")
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
    print("DEBUG top assembly file:", __file__)
    print("DEBUG incoming top_intent type:", type(top_intent).__name__)
    print("DEBUG incoming top_intent:", json.dumps(top_intent, indent=2) if isinstance(top_intent, dict) else top_intent)

    top_cfg = top_intent.get("top", {}) if isinstance(top_intent, dict) else {}

    top_base = (top_cfg.get("base_name") or state.get("soc_top_name") or state.get("top_module") or "soc_top").strip()
    top_sim = (top_cfg.get("sim_module") or f"{top_base}_sim").strip()
    top_phys = (top_cfg.get("phys_module") or f"{top_base}_phys").strip()

    instances = top_intent.get("instances", []) if isinstance(top_intent, dict) else []
    connections = top_intent.get("connections", []) if isinstance(top_intent, dict) else []
    tieoffs = top_intent.get("tieoffs", []) if isinstance(top_intent, dict) else []

    if not instances:
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            "top_assembly_bad_intent.json",
            json.dumps(top_intent, indent=2),
        )
        raise ValueError("system_integration_intent missing instances. Refusing to generate stub SoC top.")

    if not connections and not tieoffs:
        raise ValueError("system_integration_intent has no connections/tieoffs. Refusing to generate stub SoC top.")

    sim_code = _assemble_top(top_sim, top_intent, variant="sim")
    phys_code = _assemble_top(top_phys, top_intent, variant="phys")

    save_text_artifact_and_record(workflow_id, agent_name, "system/integration", f"{top_sim}.sv", sim_code)
    save_text_artifact_and_record(workflow_id, agent_name, "system/integration", f"{top_phys}.sv", phys_code)

    state["soc_top_sim_module"] = top_sim
    state["soc_top_phys_module"] = top_phys
    state["soc_top_sim_path"] = f"system/integration/{top_sim}.sv"
    state["soc_top_phys_path"] = f"system/integration/{top_phys}.sv"
    state["status"] = f"✅ Generated SoC tops: {top_sim}.sv and {top_phys}.sv"
    return state