import json
import re
import os
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

def _normalize_sv_literal(val: str, port_range: str = "") -> str:
    s = str(val).strip()

    # Already looks like a legal SV literal
    if "'" in s:
        return s

    # Hex style: 0x1234 -> 32'h1234, or sized by port range when obvious
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", s):
        hex_digits = s[2:]
        if port_range:
            m = re.fullmatch(r"\[(\d+):(\d+)\]", port_range.strip())
            if m:
                msb = int(m.group(1))
                lsb = int(m.group(2))
                width = abs(msb - lsb) + 1
                return f"{width}'h{hex_digits}"
        return f"32'h{hex_digits}"

    # Binary/decimal convenience
    if s in ("0", "1"):
        if port_range:
            m = re.fullmatch(r"\[(\d+):(\d+)\]", port_range.strip())
            if m:
                msb = int(m.group(1))
                lsb = int(m.group(2))
                width = abs(msb - lsb) + 1
                return f"{width}'d{s}"
        return f"1'b{s}"

    return s


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

def _collect_module_files(workflow_dir: str):
    rels = []
    if not workflow_dir or not os.path.isdir(workflow_dir):
        return rels

    candidate_roots = [
        os.path.join(workflow_dir, "digital"),
        os.path.join(workflow_dir, "analog"),
        os.path.join(workflow_dir, "system"),
        os.path.join(workflow_dir, "rtl"),
    ]
    for root in candidate_roots:
        if not os.path.isdir(root):
            continue
        for walk_root, _, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(".sv") or name.endswith(".v"):
                    rels.append(os.path.join(walk_root, name).replace("\\", "/"))
    return rels


def _strip_sv_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _extract_module_ports_from_text(sv_text: str):
    out = {}
    if not sv_text:
        return out

    text = _strip_sv_comments(sv_text)

    # ANSI style: module m(input clk, output logic done, input [7:0] data);
    ansi_pat = re.compile(
        r"module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\s*(?:#\s*\(.*?\))?\s*\((.*?)\)\s*;",
        re.DOTALL,
    )
    for m in ansi_pat.finditer(text):
        mod = m.group(1)
        body = m.group(2)
        ports = {}
        for decl in re.finditer(
            r"\b(input|output|inout)\b\s*(?:wire|logic|reg\s*)?(?:signed\s*)?(\[[^\]]+\])?\s*([^;,\)]+)",
            body,
            flags=re.DOTALL,
        ):
            direction = decl.group(1)
            rng = (decl.group(2) or "").strip()
            names_blob = decl.group(3)
            for raw_name in names_blob.split(","):
                nm = raw_name.strip()
                nm = re.sub(r"\s*=.*$", "", nm).strip()
                nm = re.sub(r"\[[^\]]+\]$", "", nm).strip()
                if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_$]*", nm):
                    ports[nm] = {"dir": direction, "range": rng}
        if ports:
            out[mod] = ports

    return out


def _load_module_port_db(workflow_dir: str):
    db = {}
    for abs_path in _collect_module_files(workflow_dir):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                parsed = _extract_module_ports_from_text(f.read())
            for mod, ports in parsed.items():
                db[mod] = ports
        except Exception:
            pass
    return db


def _port_alias_candidates(port: str):
    p = (port or "").strip()
    if not p:
        return []
    out = [p]
    family = {
        "rst_n": ["reset_n", "resetn", "rstn", "aresetn"],
        "reset_n": ["rst_n", "resetn", "rstn", "aresetn"],
        "rst": ["reset"],
        "reset": ["rst"],
    }
    out.extend(family.get(p, []))
    # reverse lookup
    for k, vals in family.items():
        if p in vals and k not in out:
            out.append(k)
    dedup = []
    for x in out:
        if x not in dedup:
            dedup.append(x)
    return dedup


def _resolve_real_instance_port(module_port_db: dict, module_name: str, requested_port: str):
    ports = module_port_db.get(module_name) or {}
    if requested_port in ports:
        return requested_port
    for cand in _port_alias_candidates(requested_port):
        if cand in ports:
            return cand
    return requested_port


def _range_for_instance_port(module_port_db: dict, module_name: str, port_name: str):
    ports = module_port_db.get(module_name) or {}
    meta = ports.get(port_name) or {}
    return (meta.get("range") or "").strip()


def _assemble_top(top_module: str, intent: dict, variant: str, module_port_db: dict) -> str:
    instances = intent.get("instances", [])
    connections = intent.get("connections", [])
    tieoffs = intent.get("tieoffs", [])

    variants = intent.get("variants", {}) or {}
    module_overrides = (variants.get(variant, {}) or {}).get("module_overrides", {}) or {}

    instance_module = {}
    for inst in instances:
        name = inst.get("name")
        mod = inst.get("module")
        if name and mod:
            instance_module[name] = module_overrides.get(name, mod)

    port_map = {inst["name"]: {} for inst in instances if "name" in inst}
    top_ports = {}
    wire_meta = {}
    wire_decls = []

    driven_instance_ports = set()
    driven_top_ports = set()

    # tieoffs
    for t in tieoffs:
        inst, port, val = _normalize_tieoff_entry(t)
        if not inst or not port or val is None or inst == "top":
            continue

        base, rng = _split_port_and_range(port)
        real_port = _resolve_real_instance_port(module_port_db, instance_module.get(inst, ""), base)
        real_rng = _range_for_instance_port(module_port_db, instance_module.get(inst, ""), real_port) or rng

        if inst in port_map and real_port:
            port_map[inst][real_port] = _normalize_sv_literal(str(val), real_rng)

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

        sp_req, sr = _split_port_and_range(sp_raw)
        dp_req, dr = _split_port_and_range(dp_raw)

        sp = sp_req if si == "top" else _resolve_real_instance_port(module_port_db, instance_module.get(si, ""), sp_req)
        dp = dp_req if di == "top" else _resolve_real_instance_port(module_port_db, instance_module.get(di, ""), dp_req)

        sr_real = sr if si == "top" else (_range_for_instance_port(module_port_db, instance_module.get(si, ""), sp) or sr)
        dr_real = dr if di == "top" else (_range_for_instance_port(module_port_db, instance_module.get(di, ""), dp) or dr)

        if sr_real and dr_real and sr_real != dr_real:
            raise ValueError(f"Width mismatch between {si}.{sp} ({sr_real}) and {di}.{dp} ({dr_real})")

        width = _merge_range(sr_real, dr_real)

        if si == "top" and di != "top":
            dst_key = (di, dp)
            if dst_key in driven_instance_ports:
                raise ValueError(f"Multiple drivers detected for {di}.{dp} during top assembly.")
            driven_instance_ports.add(dst_key)

            new_dir = _top_dir_for_endpoint(True)
            if sp in top_ports and top_ports[sp]["dir"] != new_dir:
                raise ValueError(f"Conflicting directions inferred for top port '{sp}'")
            top_ports[sp] = {"dir": new_dir, "range": sr_real}

            if di in port_map:
                port_map[di][dp] = sp
            continue

        if si != "top" and di == "top":
            if dp in driven_top_ports:
                raise ValueError(f"Multiple drivers detected for top port '{dp}'")
            driven_top_ports.add(dp)

            top_ports[dp] = {"dir": _top_dir_for_endpoint(False), "range": dr_real}
            if si in port_map:
                port_map[si][sp] = dp
            continue

        if si == "top" and di == "top":
            continue

        dst_key = (di, dp)
        if dst_key in driven_instance_ports:
            raise ValueError(f"Multiple drivers detected for {di}.{dp} during top assembly.")
        driven_instance_ports.add(dst_key)

        w = _wire_name(idx, f"{si}.{sp}", f"{di}.{dp}")
        wire_meta[w] = width
        if si in port_map:
            port_map[si][sp] = w
        if di in port_map:
            port_map[di][dp] = w

    for w in sorted(wire_meta.keys()):
        rng = wire_meta[w]
        wire_decls.append(f"  logic {rng} {w};" if rng else f"  logic {w};")

    lines = []
    port_items = []
    for pname in sorted(top_ports.keys()):
        meta = top_ports[pname]
        d = meta["dir"]
        r = meta["range"]
        port_items.append(f"  {d} logic {r} {pname}" if r else f"  {d} logic {pname}")

    if port_items:
        lines.append(f"module {top_module} (")
        for i, item in enumerate(port_items):
            lines.append(f"{item}{',' if i < len(port_items)-1 else ''}")
        lines.append(");")
    else:
        lines.append(f"module {top_module} ();")
    lines.append("")

    if variant == "phys":
        lines.append("  // NOTE: physical top may still use behavioral analog override if no physical macro wrapper was supplied.")
        lines.append("")

    if wire_decls:
        lines.append("  // Auto-generated interconnect wires")
        lines.extend(wire_decls)
        lines.append("")

    for inst in sorted(instances, key=lambda x: x.get("name", "")):
        name = inst.get("name")
        module = instance_module.get(name, inst.get("module"))
        if not name or not module:
            continue

        pm = port_map.get(name, {})
        if pm:
            lines.append(f"  {module} {name} (")
            keys = sorted(pm.keys())
            for k_i, port in enumerate(keys):
                rhs = pm[port]
                # ensure emitted port name matches real RTL interface
                real_port = _resolve_real_instance_port(
                    module_port_db,
                    module,
                    port
                )

                lines.append(f"    .{real_port}({rhs}){',' if k_i < len(keys)-1 else ''}")
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
    workflow_dir = state.get("workflow_dir") or ""
    if not top_intent and workflow_dir:
        intent_abs = os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json")
        if os.path.isfile(intent_abs):
            try:
                with open(intent_abs, "r", encoding="utf-8") as f:
                    top_intent = json.load(f)
            except Exception:
                top_intent = {}
    print("DEBUG top assembly file:", __file__)
    print("DEBUG incoming top_intent type:", type(top_intent).__name__)
    print("DEBUG incoming top_intent:", json.dumps(top_intent, indent=2) if isinstance(top_intent, dict) else top_intent)

    top_cfg = top_intent.get("top", {}) if isinstance(top_intent, dict) else {}

    top_base = (top_cfg.get("base_name") or state.get("soc_top_name") or state.get("top_module") or "soc_top").strip()
    top_sim = (top_cfg.get("sim_module") or f"{top_base}_sim").strip()
    top_phys = (top_cfg.get("phys_module") or f"{top_base}_phys").strip()


    # NEW: build module port database for alias resolution
    module_port_db = _load_module_port_db(workflow_dir) if workflow_dir else {}

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
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            "top_assembly_bad_intent.json",
            json.dumps({
                "reason": "no_connections_or_tieoffs",
                "intent": top_intent,
            }, indent=2),
        )
        raise ValueError("system_integration_intent has no connections/tieoffs. Refusing to generate stub SoC top.")


    top_edges = [
        c for c in connections
        if isinstance(c, dict) and (
            str(c.get("from", "")).startswith("top.") or
            str(c.get("to", "")).startswith("top.")
        )
    ]

    inst_to_inst_edges = [
        c for c in connections
        if isinstance(c, dict)
        and not str(c.get("from", "")).startswith("top.")
        and not str(c.get("to", "")).startswith("top.")
    ]

    if not top_edges and not inst_to_inst_edges:
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            "top_assembly_bad_intent.json",
            json.dumps(top_intent, indent=2),
        )
        raise ValueError(
            "system_integration_intent has no usable top-level or inter-instance connectivity. Refusing to generate trivial SoC top."
        )



    sim_code = _assemble_top(top_sim, top_intent, variant="sim", module_port_db=module_port_db)
    phys_code = _assemble_top(top_phys, top_intent, variant="phys", module_port_db=module_port_db)

    variants = top_intent.get("variants", {}) if isinstance(top_intent, dict) else {}
    sim_overrides = ((variants.get("sim") or {}).get("module_overrides") or {})
    phys_overrides = ((variants.get("phys") or {}).get("module_overrides") or {})

    if "u_analog" in sim_overrides and "u_analog" in phys_overrides:
        if sim_overrides["u_analog"] == phys_overrides["u_analog"]:
            print("⚠️ WARNING: sim and phys analog overrides are identical; physical top may still be behavioral.")

    sim_rel = f"system/integration/{top_sim}.sv"
    phys_rel = f"system/integration/{top_phys}.sv"

    # 1) Write local files for downstream agents (especially cocotb)
    workflow_dir = state.get("workflow_dir") or ""
    if workflow_dir:
        sim_abs = os.path.join(workflow_dir, sim_rel)
        phys_abs = os.path.join(workflow_dir, phys_rel)

        os.makedirs(os.path.dirname(sim_abs), exist_ok=True)
        os.makedirs(os.path.dirname(phys_abs), exist_ok=True)

        with open(sim_abs, "w", encoding="utf-8") as f:
            f.write(sim_code)

        with open(phys_abs, "w", encoding="utf-8") as f:
            f.write(phys_code)

    # 2) Upload artifacts
    save_text_artifact_and_record(workflow_id, agent_name, "system/integration", f"{top_sim}.sv", sim_code)
    save_text_artifact_and_record(workflow_id, agent_name, "system/integration", f"{top_phys}.sv", phys_code)

    state["soc_top_sim_module"] = top_sim
    state["soc_top_phys_module"] = top_phys
    state["soc_top_sim_path"] = sim_rel
    state["soc_top_phys_path"] = phys_rel
    state["system_top_sim_path"] = sim_rel
    state["system_top_phys_path"] = phys_rel



    existing_rtl = state.get("rtl_inputs") or state.get("system_rtl_files") or []
    if not isinstance(existing_rtl, list):
        existing_rtl = [existing_rtl] if existing_rtl else []

    merged_rtl = []
    for p in existing_rtl + [state["soc_top_sim_path"]]:
        if isinstance(p, str) and p.strip() and p not in merged_rtl:
            merged_rtl.append(p)

    state["rtl_inputs"] = merged_rtl
    state["system_rtl_files"] = merged_rtl

    system_block = state.setdefault("system", {})
    system_block["rtl_inputs"] = merged_rtl
    system_block["soc_top_sim_path"] = state["soc_top_sim_path"]
    system_block["soc_top_phys_path"] = state["soc_top_phys_path"]

    
    state["status"] = f"✅ Generated SoC tops: {top_sim}.sv and {top_phys}.sv"
    return state