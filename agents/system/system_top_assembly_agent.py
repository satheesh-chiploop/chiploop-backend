import json
import re
import os
import subprocess
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

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
        os.path.join(workflow_dir, "digital", "rtl_refactored"),
        os.path.join(workflow_dir, "analog"),
        os.path.join(workflow_dir, "system"),
    ]
    for root in candidate_roots:
        if not os.path.isdir(root):
            continue
        for walk_root, _, files in os.walk(root):
            for name in sorted(files):
                if not (name.endswith(".sv") or name.endswith(".v")):
                    continue

                abs_path = os.path.join(walk_root, name)
                rel_path = os.path.relpath(abs_path, workflow_dir).replace("\\", "/")

                # Do not rediscover generated SoC top assembly outputs.
                if rel_path.startswith("system/integration/") and (
                    name.endswith("_sim.sv") or
                    name.endswith("_phys.sv") or
                    name.startswith("soc_top_")
                ):
                    continue

                rels.append(rel_path)
    return rels

def _collect_lib_files(workflow_dir: str):
    """
    Discover Liberty timing library files under the workflow directory.
    These are for downstream phys/STA handoff only and must NOT be passed
    to iverilog/verilator.
    """
    hits = []
    if not workflow_dir or not os.path.isdir(workflow_dir):
        return hits

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            low = fn.lower()
            if low.endswith(".lib") or low.endswith(".lib.gz") or low.endswith(".db"):
                abs_path = os.path.join(root, fn)
                rel_path = os.path.relpath(abs_path, workflow_dir).replace("\\", "/")
                hits.append(rel_path)

    # stable order, unique
    deduped = []
    seen = set()
    for p in sorted(hits):
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped

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
    for rel_path in _collect_module_files(workflow_dir):
        abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(workflow_dir, rel_path)
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
            top_ports[sp] = {"dir": new_dir, "range": width}

            if di in port_map:
                port_map[di][dp] = sp
            continue

        if si != "top" and di == "top":
            if dp in driven_top_ports:
                raise ValueError(f"Multiple drivers detected for top port '{dp}'")
            driven_top_ports.add(dp)

            top_ports[dp] = {"dir": _top_dir_for_endpoint(False), "range": width}
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

def _abs_existing_rtl_files(workflow_dir: str, rels):
    out = []
    for p in rels or []:
        if not isinstance(p, str) or not p.strip():
            continue
        abs_p = p if os.path.isabs(p) else os.path.join(workflow_dir, p)
        if os.path.isfile(abs_p) and abs_p not in out:
            out.append(abs_p)
    return out

def _filter_analog_behavioral_for_phys(rel_paths):
    """
    Phys compile should exclude analog behavioral RTL.
    Keep digital RTL, the generated phys top, and only analog stub/wrapper/macro-style RTL
    when such a physical-facing Verilog view exists.
    """
    out = []
    for p in rel_paths or []:
        norm = str(p).replace("\\", "/").lower()
        base = os.path.basename(norm)

        if norm.startswith("analog/"):
            # Keep only explicit physical-facing analog views in RTL form.
            keep_analog = (
                "_stub." in base or
                "_wrapper." in base or
                "blackbox" in base or
                "_macro." in base or
                base.startswith("macro_")
            )
            if not keep_analog:
                continue

        out.append(p)
    return out

def _build_system_filelists(workflow_dir: str, existing_rtl, discovered_rtl, sim_top_rel: str, phys_top_rel: str):
    existing_rtl = existing_rtl or []
    discovered_rtl = discovered_rtl or []

    base_rel = []
    for p in existing_rtl + discovered_rtl:
        if not isinstance(p, str) or not p.strip():
            continue
        norm = p.replace("\\", "/")
        if norm in (sim_top_rel, phys_top_rel):
            continue
        if norm not in base_rel:
            base_rel.append(norm)

    sim_rel = list(base_rel)
    sim_rel.append(sim_top_rel)

    phys_base = _filter_analog_behavioral_for_phys(base_rel)
    phys_rel = list(phys_base)
    phys_rel.append(phys_top_rel)

    sim_abs = _abs_existing_rtl_files(workflow_dir, sim_rel)
    phys_abs = _abs_existing_rtl_files(workflow_dir, phys_rel)

    return sim_rel, sim_abs, phys_rel, phys_abs

def _build_system_lib_filelist(workflow_dir: str, discovered_libs):
    discovered_libs = discovered_libs or []

    lib_rel = []
    for p in discovered_libs:
        if isinstance(p, str) and p.strip() and p not in lib_rel:
            lib_rel.append(p)

    lib_abs = []
    for p in lib_rel:
        ap = p if os.path.isabs(p) else os.path.join(workflow_dir, p)
        ap = os.path.abspath(ap)
        if os.path.exists(ap):
            lib_abs.append(ap)

    return lib_rel, lib_abs


def _has_non_top_analog_rtl(rtl_files, top_abs):
    top_abs = os.path.abspath(top_abs)
    for p in rtl_files or []:
        ap = os.path.abspath(p)
        norm = ap.replace("\\", "/").lower()
        if ap == top_abs:
            continue
        if "/analog/" in norm and (norm.endswith(".v") or norm.endswith(".sv")):
            return True
    return False


def _run_full_compile_pair(workflow_id, agent_name, workflow_dir, tag, top_module, rtl_files):
    iverilog_ok, iverilog_log = _run_iverilog_full_compile(workflow_dir, top_module, rtl_files)
    verilator_ok, verilator_log = _run_verilator_full_lint(workflow_dir, top_module, rtl_files)

    save_text_artifact_and_record(
        workflow_id, agent_name, "system/integration",
        f"system_full_compile_iverilog_{tag}.log", iverilog_log
    )
    save_text_artifact_and_record(
        workflow_id, agent_name, "system/integration",
        f"system_full_compile_verilator_{tag}.log", verilator_log
    )

    return iverilog_ok, iverilog_log, verilator_ok, verilator_log


def _run_pass2_for_top(
    workflow_id,
    agent_name,
    workflow_dir,
    rel_path,
    top_module,
    top_intent,
    rtl_files,
    tag,
    iverilog_log,
    verilator_log,
):
    top_abs = os.path.join(workflow_dir, rel_path)
    with open(top_abs, "r", encoding="utf-8") as f:
        pass1_top_code = f.read()

    repaired_code, repair_prompt = _run_system_top_pass2_repair(
        top_module=top_module,
        top_code=pass1_top_code,
        intent=top_intent,
        rtl_files=rtl_files,
        iverilog_log=iverilog_log,
        verilator_log=verilator_log,
    )

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        f"system_top_pass2_repair_prompt_{tag}.txt",
        repair_prompt
    )

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        f"{top_module}_pass2_candidate.sv",
        repaired_code
    )

    with open(top_abs, "w", encoding="utf-8") as f:
        f.write(repaired_code)

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        f"{top_module}.sv",
        repaired_code
    )

    ok_iverilog_2, iverilog_log_2, ok_verilator_2, verilator_log_2 = _run_full_compile_pair(
        workflow_id=workflow_id,
        agent_name=agent_name,
        workflow_dir=workflow_dir,
        tag=f"{tag}_pass2",
        top_module=top_module,
        rtl_files=rtl_files,
    )

    return {
        "iverilog_ok_pass2": ok_iverilog_2,
        "verilator_ok_pass2": ok_verilator_2,
        "iverilog_log_pass2": iverilog_log_2,
        "verilator_log_pass2": verilator_log_2,
    }

def _run_iverilog_full_compile(workflow_dir: str, top_module: str, rtl_files):
    log_path = os.path.join(workflow_dir, "system", "integration", "system_full_compile_iverilog.log")
    cmd = [
        "iverilog",
        "-g2012",
        "-s", top_module,
        "-o", os.path.join(workflow_dir, "system", "integration", f"{top_module}.out"),
        *rtl_files,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=workflow_dir)
        text = f"$ {' '.join(cmd)}\n\nSTDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
        return proc.returncode == 0, text
    except Exception as e:
        text = f"Iverilog invocation failed: {e}"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
        return False, text




def _run_verilator_full_lint(workflow_dir: str, top_module: str, rtl_files):
    log_path = os.path.join(workflow_dir, "system", "integration", "system_full_compile_verilator.log")
    cmd = [
        "verilator",
        "--lint-only",
        "--timing",
        "-Wall",
        "-Wno-fatal",
        "--top-module", top_module,
        *rtl_files,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=workflow_dir)
        text = f"$ {' '.join(cmd)}\n\nSTDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
        return proc.returncode == 0, text
    except Exception as e:
        text = f"Verilator invocation failed: {e}"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
        return False, text

def _tail_text(s: str, max_chars: int = 12000) -> str:
    s = s or ""
    return s[-max_chars:] if len(s) > max_chars else s

def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

def _build_system_rtl_repair_prompt(
    top_module: str,
    top_code: str,
    intent: dict,
    rtl_files: list,
    iverilog_log: str,
    verilator_log: str,
) -> str:
    return f"""
You are repairing a generated SystemVerilog SoC top module.

GOAL:
- Fix ONLY the top-level integration module.
- Do NOT modify digital or analog leaf RTL.
- Preserve architecture intent.
- Make the design compile with Icarus Verilog.
- Make the design pass Verilator lint-only as much as possible.
- Prefer minimal edits.

TOP MODULE NAME:
{top_module}

SYSTEM INTEGRATION INTENT JSON:
{json.dumps(intent, indent=2)}

RTL FILELIST:
{json.dumps(rtl_files, indent=2)}

CURRENT TOP MODULE CODE:
```systemverilog
{top_code}
```

IVERILOG LOG:
```text
{_tail_text(iverilog_log)}
```

VERILATOR LOG:
```text
{_tail_text(verilator_log)}
```

STRICT RULES:
- Respect actual port directions.
- Do not invent new instances.
- Do not rename u_digital or u_analog.
- Do not modify leaf module RTL.
- Keep only one top module in output.
- Return ONLY corrected SystemVerilog code for the top module.
"""


def _run_system_top_pass2_repair(
    top_module: str,
    top_code: str,
    intent: dict,
    rtl_files: list,
    iverilog_log: str,
    verilator_log: str,
):
    prompt = _build_system_rtl_repair_prompt(
        top_module=top_module,
        top_code=top_code,
        intent=intent,
        rtl_files=rtl_files,
        iverilog_log=iverilog_log,
        verilator_log=verilator_log,
    )

    repaired_code = llm_text(prompt)
    repaired_code = _strip_code_fences(repaired_code)

    if not repaired_code.strip():
        raise RuntimeError("System RTL Pass2 repair returned empty output")

    if f"module {top_module}" not in repaired_code:
        raise RuntimeError(
            f"System RTL Pass2 repair did not return expected top module '{top_module}'"
        )

    return repaired_code, prompt




def run_agent(state: dict) -> dict:
    agent_name = "System Top Assembly Agent"
    workflow_id = state.get("workflow_id")

    top_intent = state.get("system_integration_intent") or {}

    workflow_dir = os.path.abspath(state.get("workflow_dir") or "")
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

    if workflow_dir:
        sim_abs = os.path.join(workflow_dir, sim_rel)
        phys_abs = os.path.join(workflow_dir, phys_rel)

        os.makedirs(os.path.dirname(sim_abs), exist_ok=True)
        os.makedirs(os.path.dirname(phys_abs), exist_ok=True)

        with open(sim_abs, "w", encoding="utf-8") as f:
            f.write(sim_code)

        with open(phys_abs, "w", encoding="utf-8") as f:
            f.write(phys_code)

        # ✅ SAVE PASS1 TOP BEFORE ANY COMPILE OR REPAIR
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            f"{top_sim}_pass1.sv",
            sim_code
        )

        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            f"{top_phys}_pass1.sv",
            phys_code
        )

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

    discovered_rtl = []

    if workflow_dir:
        discovered_rtl = [
            p.replace("\\", "/")
            for p in _collect_module_files(workflow_dir)
        ]
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            "system_discovered_rtl.txt",
            "\n".join(discovered_rtl) if discovered_rtl else "(empty)"
        )


    sim_rel_list, sim_abs_list, phys_rel_list, phys_abs_list = _build_system_filelists(
        workflow_dir=workflow_dir,
        existing_rtl=existing_rtl,
        discovered_rtl=discovered_rtl,
        sim_top_rel=state["soc_top_sim_path"],
        phys_top_rel=state["soc_top_phys_path"],
    )


    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        "system_rtl_filelist_sim.txt",
        "\n".join(sim_abs_list) if sim_abs_list else "(empty)"
    )

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        "system_rtl_filelist_phys.txt",
        "\n".join(phys_abs_list) if phys_abs_list else "(empty)"
    )


    discovered_libs = []
    state_lib_candidates = []

    analog_macro_lib = state.get("analog_macro_lib")
    if analog_macro_lib:
        state_lib_candidates.append(analog_macro_lib)

    system_block = state.get("system") or {}
    state_lib_candidates.extend(system_block.get("lib_filelist_phys") or [])
    state_lib_candidates.extend(state.get("system_lib_filelist_phys") or [])

    if workflow_dir:
        discovered_libs = [
            p.replace("\\", "/")
            for p in _collect_lib_files(workflow_dir)
        ]

    # normalize explicit state lib paths into rel paths when possible
    explicit_rel_libs = []
    for p in state_lib_candidates:
        if not p:
            continue
        abs_p = p if os.path.isabs(p) else os.path.join(workflow_dir, p)
        abs_p = os.path.abspath(abs_p)
        if os.path.exists(abs_p):
            try:
                explicit_rel_libs.append(os.path.relpath(abs_p, workflow_dir).replace("\\", "/"))
            except Exception:
                explicit_rel_libs.append(abs_p)

    merged_discovered_libs = []
    for p in explicit_rel_libs + discovered_libs:
        if p not in merged_discovered_libs:
            merged_discovered_libs.append(p)

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        "system_discovered_libs.txt",
        "\n".join(merged_discovered_libs) if merged_discovered_libs else "(empty)"
    )

    phys_lib_rel_list, phys_lib_abs_list = _build_system_lib_filelist(
        workflow_dir=workflow_dir,
        discovered_libs=merged_discovered_libs,
    )

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        "system_lib_filelist_phys.txt",
        "\n".join(phys_lib_abs_list) if phys_lib_abs_list else "(empty)"
    )

    state["system_lib_filelist_phys"] = phys_lib_abs_list
    system_block = state.setdefault("system", {})
    system_block["lib_filelist_phys"] = phys_lib_abs_list
    state["system_rtl_filelist_sim"] = sim_abs_list
    state["system_rtl_filelist_phys"] = phys_abs_list

    if workflow_dir and not sim_abs_list:
        summary = {
            "sim": {
                "top_module": top_sim,
                "rtl_files": [],
                "iverilog_ok": False,
                "verilator_ok": False,
                "error": "No RTL files found for sim full compile"
            },
            "phys": {
                "top_module": top_phys,
                "rtl_files": phys_abs_list,
                "skipped": True,
                "reason": "Sim filelist missing; phys not attempted"
            }
        }
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "system/integration",
            "system_full_compile_summary.json",
            json.dumps(summary, indent=2)
        )
        state["system_full_compile_ok"] = False
        state["system_full_compile_summary"] = summary
        state["status"] = "⚠️ No RTL files found for sim full compile"
        return state

    summary = {
        "sim": {
            "top_module": top_sim,
            "rtl_files": sim_abs_list,
        },
        "phys": {
            "top_module": top_phys,
            "rtl_files": phys_abs_list,
            "lib_files": phys_lib_abs_list,
        }
    }

    if workflow_dir and sim_abs_list:
        sim_iv_ok, sim_iv_log, sim_vlt_ok, sim_vlt_log = _run_full_compile_pair(
            workflow_id=workflow_id,
            agent_name=agent_name,
            workflow_dir=workflow_dir,
            tag="sim_pass1",
            top_module=top_sim,
            rtl_files=sim_abs_list,
        )

        summary["sim"]["iverilog_ok_pass1"] = sim_iv_ok
        summary["sim"]["verilator_ok_pass1"] = sim_vlt_ok

        if not sim_iv_ok or not sim_vlt_ok:
            sim_pass2 = _run_pass2_for_top(
                workflow_id=workflow_id,
                agent_name=agent_name,
                workflow_dir=workflow_dir,
                rel_path=sim_rel,
                top_module=top_sim,
                top_intent=top_intent,
                rtl_files=sim_abs_list,
                tag="sim",
                iverilog_log=sim_iv_log,
                verilator_log=sim_vlt_log,
            )
            summary["sim"].update({
                "iverilog_ok_pass2": sim_pass2["iverilog_ok_pass2"],
                "verilator_ok_pass2": sim_pass2["verilator_ok_pass2"],
            })

    phys_top_abs = os.path.join(workflow_dir, phys_rel) if workflow_dir else ""
    phys_has_analog_rtl = _has_non_top_analog_rtl(phys_abs_list, phys_top_abs)

    if workflow_dir and phys_abs_list and phys_has_analog_rtl:
        phys_iv_ok, phys_iv_log, phys_vlt_ok, phys_vlt_log = _run_full_compile_pair(
            workflow_id=workflow_id,
            agent_name=agent_name,
            workflow_dir=workflow_dir,
            tag="phys_pass1",
            top_module=top_phys,
            rtl_files=phys_abs_list,
        )

        summary["phys"]["iverilog_ok_pass1"] = phys_iv_ok
        summary["phys"]["verilator_ok_pass1"] = phys_vlt_ok

        if not phys_iv_ok or not phys_vlt_ok:
            phys_pass2 = _run_pass2_for_top(
                workflow_id=workflow_id,
                agent_name=agent_name,
                workflow_dir=workflow_dir,
                rel_path=phys_rel,
                top_module=top_phys,
                top_intent=top_intent,
                rtl_files=phys_abs_list,
                tag="phys",
                iverilog_log=phys_iv_log,
                verilator_log=phys_vlt_log,
            )
            summary["phys"].update({
                "iverilog_ok_pass2": phys_pass2["iverilog_ok_pass2"],
                "verilator_ok_pass2": phys_pass2["verilator_ok_pass2"],
            })
    else:
        summary["phys"]["skipped"] = True
        summary["phys"]["reason"] = (
            "Phys compile skipped because no non-top analog RTL/stub/wrapper was found "
            "after excluding analog behavioral model. Liberty files were emitted in "
            "system_lib_filelist_phys.txt for downstream handoff/STA flow and must not "
            "be passed to iverilog/verilator."
        )

    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/integration",
        "system_full_compile_summary.json",
        json.dumps(summary, indent=2)
    )

    sim_ok = (
        summary["sim"].get("iverilog_ok_pass2", summary["sim"].get("iverilog_ok_pass1", False))
        and summary["sim"].get("verilator_ok_pass2", summary["sim"].get("verilator_ok_pass1", False))
    )

    phys_ok = True
    if not summary["phys"].get("skipped"):
        phys_ok = (
            summary["phys"].get("iverilog_ok_pass2", summary["phys"].get("iverilog_ok_pass1", False))
            and summary["phys"].get("verilator_ok_pass2", summary["phys"].get("verilator_ok_pass1", False))
        )

    state["system_full_compile_ok"] = bool(sim_ok and phys_ok)
    state["system_full_compile_summary"] = summary

    if not sim_ok:
        state["status"] = (
            f"⚠️ Generated SoC tops: {top_sim}.sv and {top_phys}.sv, "
            f"but sim full RTL compile/lint failed after Pass2 repair"
        )
        return state

    if not summary["phys"].get("skipped") and not phys_ok:
        state["status"] = (
            f"⚠️ Generated SoC tops: {top_sim}.sv and {top_phys}.sv, "
            f"sim passed, but phys full RTL compile/lint failed after Pass2 repair"
        )
        return state

    # Keep sim RTL as the default "rtl_inputs" because downstream simulation-style
    # consumers typically expect compilable RTL including the analog behavioral model.
    state["rtl_inputs"] = sim_abs_list
    state["system_rtl_files"] = sim_abs_list

    system_block = state.setdefault("system", {})
    system_block["rtl_inputs"] = sim_abs_list
    system_block["rtl_filelist_sim"] = sim_abs_list
    system_block["rtl_filelist_phys"] = phys_abs_list
    system_block["lib_filelist_phys"] = phys_lib_abs_list
    system_block["soc_top_sim_path"] = state["soc_top_sim_path"]
    system_block["soc_top_phys_path"] = state["soc_top_phys_path"]

    state["status"] = f"✅ Generated SoC tops: {top_sim}.sv and {top_phys}.sv"
    return state

    