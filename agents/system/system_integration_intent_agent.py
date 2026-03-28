import os
import json
import datetime
import requests
import re
from collections import defaultdict
from typing import Dict

from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


# ---------------------------------------------------------------------
# System Loop: Integration Intent Agent
# - Generates a strict JSON integration manifest for SoC top assembly.
# - Designed to avoid touching digital_integration_intent_agent.py.
# ---------------------------------------------------------------------

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY) if PORTKEY_API_KEY else None
client_openai = OpenAI()

def _normalize_dir_token(d: str):
    d = str(d or "").lower().strip()
    if d in ("in", "input"):
        return "input"
    if d in ("out", "output"):
        return "output"
    if d in ("inout", "io"):
        return "inout"
    return None


def _parse_simple_module_ports_from_rtl_text(text: str):
    """
    Very lightweight RTL parser:
    - extracts first module name
    - extracts header-style ports like:
        input clk,
        output logic adc_done,
        input [9:0] adc_data
    This is intentionally simple and generic.
    """
    if not text:
        return None, []

    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*;", text, re.S)
    if not m:
        return None, []

    module_name = m.group(1).strip()
    header = m.group(2)

    ports = []
    for raw in header.split(","):
        line = " ".join(raw.replace("\n", " ").split()).strip()
        if not line:
            continue

        dm = re.match(
            r"^(input|output|inout)\s+"
            r"(?:(?:wire|reg|logic|signed)\s+)*"
            r"(?:\[[^\]]+\]\s+)?"
            r"([A-Za-z_]\w*)$",
            line,
            re.I,
        )
        if dm:
            ports.append({
                "name": dm.group(2).strip(),
                "direction": _normalize_dir_token(dm.group(1)),
            })

    return module_name, ports


def _extract_connection_candidates_from_text(raw: str):
    out = []
    if not raw:
        return out

    text = raw.replace("\r\n", "\n")

    pair_pat = re.compile(
        r'(?is)(?:^|\n)\s*[-*]?\s*(?:from|src|source)\s*:\s*(.+?)\s*'
        r'(?:\n\s*(?:to|dst|dest|destination)\s*:\s*(.+?))(?=\n\s*(?:-|[*]|\w+\s*:)|\Z)'
    )
    for m in pair_pat.finditer(text):
        src = _coerce_endpoint_token(m.group(1).strip().strip('"').strip("'"))
        dst = _coerce_endpoint_token(m.group(2).strip().strip('"').strip("'"))
        if src and dst:
            out.append({"from": src, "to": dst})

    list_pat = re.compile(r'(?is)(?:^|\n)\s*[-*]?\s*(endpoints|ports)\s*:\s*\[(.*?)\]')
    for m in list_pat.finditer(text):
        key = m.group(1).strip()
        items = [s.strip().strip('"').strip("'") for s in m.group(2).split(",") if s.strip()]
        if len(items) >= 2:
            out.append({key: items})

    return out


def _fallback_intent_from_raw_text(raw: str, top_base: str, digital_module: str, analog_phys_module: str, schema: dict):
    return {
        "top": {
            "base_name": top_base,
            "sim_module": f"{top_base}_sim",
            "phys_module": f"{top_base}_phys",
        },
        "instances": [
            {"name": "u_digital", "module": digital_module},
            {"name": "u_analog", "module": analog_phys_module},
        ],
        "connections": _extract_connection_candidates_from_text(raw),
        "tieoffs": [],
        "variants": {
            "sim": schema["variants"]["sim"],
            "phys": schema["variants"]["phys"],
        },
        "notes": ["Recovered integration intent from raw text fallback parser."],
    }


def _load_rtl_text_if_exists(path: str):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def _normalize_connection_entries(intent: dict, digital_sigs: dict, analog_sigs: dict):
    """
    Convert mixed connection styles into canonical point-to-point edges:
      {"from": "...", "to": "..."}
    Supports:
      1) existing point-to-point entries
      2) net-style entries:
         {"net":"clk","ports":["top.clk","u_digital.clk","u_analog.clk"]}
    """
    if not isinstance(intent, dict):
        return intent

    inst2mod = _instance_to_module(intent)
    normalized = []
    seen = set()

    def add_edge(src: str, dst: str):
        key = (src, dst)
        if key in seen:
            return
        seen.add(key)
        normalized.append({"from": src, "to": dst})

    for c in intent.get("connections", []):
        if not isinstance(c, dict):
            continue

       
        # Case 1: point-to-point, including alias keys and dict endpoint objects
        src = c.get("from") or c.get("src") or c.get("source")
        dst = c.get("to") or c.get("dst") or c.get("dest") or c.get("destination")

        if src and dst:
            src_ep = _coerce_endpoint_token(src)
            dst_ep = _coerce_endpoint_token(dst)
            if src_ep and dst_ep:
                add_edge(src_ep, dst_ep)
                continue

        # Case 1b: source + sinks
        src_many = c.get("source") or c.get("src") or c.get("from")
        sinks = c.get("sinks") or c.get("dests") or c.get("destinations")
        if src_many and isinstance(sinks, list):
            src_ep = _coerce_endpoint_token(src_many)
            if src_ep:
                for s in sinks:
                    dst_ep = _coerce_endpoint_token(s)
                    if dst_ep:
                        add_edge(src_ep, dst_ep)
                continue
     

        # Case 2: net-style multi-port connection
        # Case 2: net-style multi-port connection using "ports"

        ports = c.get("ports")
        if isinstance(ports, list) and len(ports) >= 2:
            parsed = []
            for ep in ports:
                ep_str = _coerce_endpoint_token(ep)
                inst, port = _parse_ep(ep_str) if ep_str else (None, None)
                if inst and port:
                    parsed.append((ep_str, inst, port))


            if len(parsed) >= 2:
                tops = [ep for ep, inst, port in parsed if inst == "top"]
                non_tops = [(ep, inst, port) for ep, inst, port in parsed if inst != "top"]

                for top_ep in tops:
                    for ep, inst, port in non_tops:
                        ddir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
                        if ddir in ("input", "inout", None):
                            add_edge(top_ep, ep)

                for ep, inst, port in non_tops:
                    sdir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
                    if sdir in ("output", "inout"):
                        for top_ep in tops:
                            add_edge(ep, top_ep)

                for src_ep, src_inst, src_port in non_tops:
                    sdir = _resolve_port_dir(src_inst, src_port, inst2mod, digital_sigs, analog_sigs)
                    for dst_ep, dst_inst, dst_port in non_tops:
                        if src_ep == dst_ep or src_inst == dst_inst:
                            continue
                        ddir = _resolve_port_dir(dst_inst, dst_port, inst2mod, digital_sigs, analog_sigs)
                        if sdir in ("output", "inout") and ddir in ("input", "inout", None):
                            add_edge(src_ep, dst_ep)
                continue

        # Case 3: net-style connection using "endpoints"
        endpoints = c.get("endpoints")
        if isinstance(endpoints, list) and len(endpoints) >= 2:
            tops = []
            non_tops = []

            for ep in endpoints:

                # Case A — string endpoint: "u_inst.port"
                if isinstance(ep, str):
                    inst, port = _parse_ep(ep)
                    if inst == "top":
                        tops.append(ep)
                    elif inst and port:
                        non_tops.append((ep, inst, port))
                    continue

                # Case B — dict endpoint
                if isinstance(ep, dict):
                    if ep.get("top_port"):
                        tops.append(f'top.{ep["top_port"]}')
                    elif ep.get("instance") and ep.get("port"):
                        non_tops.append(
                            (f'{ep["instance"]}.{ep["port"]}', ep["instance"], ep["port"])
                        )

            if tops or non_tops:
                for top_ep in tops:
                    for full_ep, inst, port in non_tops:
                        ddir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
                        if ddir in ("input", "inout", None):
                            add_edge(top_ep, full_ep)

                for full_ep, inst, port in non_tops:
                    sdir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
                    if sdir in ("output", "inout"):
                        for top_ep in tops:
                            add_edge(full_ep, top_ep)

                for src_ep, src_inst, src_port in non_tops:
                    sdir = _resolve_port_dir(src_inst, src_port, inst2mod, digital_sigs, analog_sigs)
                    for dst_ep, dst_inst, dst_port in non_tops:
                        if src_ep == dst_ep or src_inst == dst_inst:
                            continue
                        ddir = _resolve_port_dir(dst_inst, dst_port, inst2mod, digital_sigs, analog_sigs)
                        if sdir in ("output", "inout") and ddir in ("input", "inout", None):
                            add_edge(src_ep, dst_ep)
                continue

        parsed = []
        ports = c.get("ports") or []
        for ep in ports:
            inst, port = _parse_ep(ep)
            if inst and port:
                parsed.append((ep, inst, port))

        if len(parsed) < 2:
            continue

        tops = [ep for ep, inst, port in parsed if inst == "top"]
        non_tops = [(ep, inst, port) for ep, inst, port in parsed if inst != "top"]

        # top -> instance(input/inout)
        for top_ep in tops:
            for ep, inst, port in non_tops:
                ddir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
                if ddir in ("input", "inout", None):
                    add_edge(top_ep, ep)

        # instance(output/inout) -> top
        for ep, inst, port in non_tops:
            sdir = _resolve_port_dir(inst, port, inst2mod, digital_sigs, analog_sigs)
            if sdir in ("output", "inout"):
                for top_ep in tops:
                    add_edge(ep, top_ep)

        # instance -> instance
        for src_ep, src_inst, src_port in non_tops:
            sdir = _resolve_port_dir(src_inst, src_port, inst2mod, digital_sigs, analog_sigs)
            for dst_ep, dst_inst, dst_port in non_tops:
                if src_ep == dst_ep or src_inst == dst_inst:
                    continue
                ddir = _resolve_port_dir(dst_inst, dst_port, inst2mod, digital_sigs, analog_sigs)
                if sdir in ("output", "inout") and ddir in ("input", "inout", None):
                    add_edge(src_ep, dst_ep)

    intent["connections"] = normalized
    return intent


def _discover_signature_from_workflow_dir(workflow_dir: str, rel_candidates):
    """
    Build a minimal signature DB from real RTL text if JSON signatures are missing.
    Returns shape:
      { "<module_name>": { "ports": [ ... ] } }
    """
    for rel in rel_candidates:
        p = os.path.join(workflow_dir, rel)
        text = _load_rtl_text_if_exists(p)
        if not text:
            continue

        mod, ports = _parse_simple_module_ports_from_rtl_text(text)
        if mod and ports:
            return {
                mod: {
                    "ports": ports
                }
            }
    return {}

def _discover_signatures_under(workflow_dir: str, subdir_name: str):
    """
    Scan an entire subtree (e.g. digital/ or analog/) and build:
      { "<module_name>": { "ports": [ ... ] } }
    """
    sigs = {}

    root_dir = os.path.join(workflow_dir, subdir_name)
    if not os.path.isdir(root_dir):
        return sigs

    for root, _, files in os.walk(root_dir):
        for fn in files:
            if not fn.lower().endswith((".v", ".sv")):
                continue

            p = os.path.join(root, fn)
            text = _load_rtl_text_if_exists(p)
            if not text:
                continue

            mod, ports = _parse_simple_module_ports_from_rtl_text(text)
            if mod and ports:
                sigs[mod] = {"ports": ports}

    return sigs

def _collect_ports_for_module(sig_db: dict, module_name: str):
    if not isinstance(sig_db, dict) or not module_name:
        return []

    if module_name in sig_db and isinstance(sig_db[module_name], dict):
        return sig_db[module_name].get("ports") or sig_db[module_name].get("interface") or []

    mods = sig_db.get("modules")
    if isinstance(mods, dict) and module_name in mods and isinstance(mods[module_name], dict):
        return mods[module_name].get("ports") or mods[module_name].get("interface") or []

    return []


def _normalize_port_name(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().lower()
    n = n.split("[", 1)[0]
    n = re.sub(r"^(i_|o_|io_|in_|out_)", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def _port_width(port: dict):
    if not isinstance(port, dict):
        return None
    for k in ("width", "bits", "size"):
        v = port.get(k)
        if isinstance(v, int):
            return v
    msb = port.get("msb")
    lsb = port.get("lsb")
    if isinstance(msb, int) and isinstance(lsb, int):
        return abs(msb - lsb) + 1
    return None

def _classify_top_port(norm_name: str):
    if not norm_name:
        return None

    if "clk" in norm_name or "clock" in norm_name:
        return "clock"

    if "rst" in norm_name or "reset" in norm_name:
        return "reset"

    if norm_name in {"vdd", "vss", "gnd", "vcc", "vin", "vref", "avdd", "dvdd", "avss", "dvss"}:
        return "power"

    return None

def _canonical_top_port_name(port_name: str) -> str:
    n = _normalize_port_name(port_name)

    if "clk" in n or "clock" in n:
        return "clk"

    # normalize reset-family names
    if "rstn" in n or "resetn" in n or n in {"rst_n", "reset_n"}:
        return "rst_n"
    if "rst" in n or "reset" in n:
        return "rst"

    return str(port_name).split("[", 1)[0].strip()


def _get_instance_ports(intent: dict, inst_name: str, digital_sigs: dict, analog_sigs: dict):
    inst2mod = _instance_to_module(intent)
    mod = inst2mod.get(inst_name)
    if not mod:
        return []
    return _collect_ports_for_module(digital_sigs, mod) or _collect_ports_for_module(analog_sigs, mod)

SEMANTIC_ALIAS = {
    "start": ["start", "enable", "trigger", "go", "req", "kick"],
    "data": ["data", "result", "value", "sample", "payload"],
    "valid": ["valid", "done", "ready", "ack"],
    "status": ["status", "state", "flag"],
    "irq": ["irq", "intr", "interrupt"],
}

def _semantic_group(name: str) -> str:
    n = str(name or "").lower()
    for group, aliases in SEMANTIC_ALIAS.items():
        if any(alias in n for alias in aliases):
            return group
    return n


def _build_deterministic_rescue_connections(intent: dict, digital_sigs: dict, analog_sigs: dict):


     
    inst2mod = _instance_to_module(intent)


    instances = list(inst2mod.keys())
    if len(instances) < 2:
        return [], []

    inst_a = "u_digital" if "u_digital" in inst2mod else instances[0]
    inst_b = "u_analog" if "u_analog" in inst2mod else next(
        (x for x in instances if x != inst_a),
        instances[1]
    )


    


    d_ports = _get_instance_ports(intent, inst_a, digital_sigs, analog_sigs)
    a_ports = _get_instance_ports(intent, inst_b, digital_sigs, analog_sigs)

    connections = []
    seen = set()
    exposed_top_ports = set()
    driven_inputs = set()

    def add_conn(src: str, dst: str):
        key = (src, dst)
        if key in seen:
            return
        seen.add(key)
        connections.append({"from": src, "to": dst})

    def pname(p):
        return str(p.get("name") or "").split("[", 1)[0].strip()

    def pdir(p):
        return str(p.get("direction") or p.get("dir") or "").lower().strip()

    def pnorm(p):
        return _normalize_port_name(pname(p))

    def pwidth(p):
        return _port_width(p)

    # 1) Top fanout for infra ports
    for ports, inst in ((d_ports, inst_a), (a_ports, inst_b)):
        for p in ports:
            if not isinstance(p, dict):
                continue
            name = pname(p)
            direction = pdir(p)
            if direction not in ("input", "inout"):
                continue
            if _classify_top_port(pnorm(p)):
                top_name = _canonical_top_port_name(name)
                add_conn(f"top.{top_name}", f"{inst}.{name}")
                driven_inputs.add((inst, name))
                exposed_top_ports.add(name)

    # 2) Deterministic digital -> analog wiring
    a_inputs = [p for p in a_ports if isinstance(p, dict) and pdir(p) in ("input", "inout")]
    d_outputs = [p for p in d_ports if isinstance(p, dict) and pdir(p) in ("output", "inout")]
    d_inputs  = [p for p in d_ports if isinstance(p, dict) and pdir(p) in ("input", "inout")]
    a_outputs = [p for p in a_ports if isinstance(p, dict) and pdir(p) in ("output", "inout")]

    def match_pairs(src_inst, src_ports, dst_inst, dst_ports):
        for sp in src_ports:
            sname = pname(sp)
            snorm = pnorm(sp)
            sw = pwidth(sp)

            candidates = []
            for dp in dst_ports:
                dname = pname(dp)
                dnorm = pnorm(dp)
                dw = pwidth(dp)

                if sw is not None and dw is not None and sw != dw:
                    continue

                exact = (snorm == dnorm)
                semantic = (_semantic_group(snorm) == _semantic_group(dnorm))
                if exact or semantic:
                    candidates.append(dp)

            if len(candidates) == 1:
                dp = candidates[0]
                dname = pname(dp)
                if (dst_inst, dname) not in driven_inputs:
                    add_conn(f"{src_inst}.{sname}", f"{dst_inst}.{dname}")
                    driven_inputs.add((dst_inst, dname))

    match_pairs(inst_a, d_outputs, inst_b, a_inputs)
    match_pairs(inst_b, a_outputs, inst_a, d_inputs)

    # 3) Expose remaining digital inputs to top
    for p in d_inputs:
        name = pname(p)
        if _classify_top_port(pnorm(p)):
            continue
        if (inst_a, name) not in driven_inputs:
            top_name = _canonical_top_port_name(name)
            add_conn(f"top.{top_name}", f"{inst_a}.{name}")
            driven_inputs.add((inst_a, name))
            exposed_top_ports.add(name)

    # 4) Expose digital outputs to top

    for p in d_outputs:
        name = pname(p)
        if _classify_top_port(pnorm(p)):
            continue
        top_name = _canonical_top_port_name(name)
        add_conn(f"{inst_a}.{name}", f"top.{top_name}")
        exposed_top_ports.add(name)

    return connections, sorted(exposed_top_ports)


def _build_generic_fallback_connections(intent: dict, digital_sigs: dict, analog_sigs: dict):
    """
    Backward-compatible generic fallback:
    - only runs if sanitize removed all connections
    - only adds safe, direction-aware, unambiguous connections
    - no design-specific hardcoding
    """
    inst2mod = _instance_to_module(intent)
    instances = list(inst2mod.keys())
    if not instances:
        return []

    connections = []
    seen = set()

    def add_conn(src: str, dst: str):
        key = (src, dst)
        if key in seen:
            return
        seen.add(key)
        connections.append({"from": src, "to": dst})

    inst_ports = {}
    for inst in instances:
        inst_ports[inst] = _get_instance_ports(intent, inst, digital_sigs, analog_sigs)

    # 1) top infrastructure fanout for obvious shared ports
    top_candidates = set()
    for inst, ports in inst_ports.items():
        for p in ports:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "").split("[", 1)[0].strip()
            pdir = str(p.get("direction") or p.get("dir") or "").lower().strip()
            cls = _classify_top_port(_normalize_port_name(pname))
            if cls and pdir in ("input", "inout"):
                top_candidates.add(_canonical_top_port_name(pname))

    for top_port in sorted(top_candidates):
        for inst, ports in inst_ports.items():
            for p in ports:
                if not isinstance(p, dict):
                    continue
                pname = str(p.get("name") or "").split("[", 1)[0].strip()
                pdir = str(p.get("direction") or p.get("dir") or "").lower().strip()
                canonical_top = _canonical_top_port_name(pname)
                if top_port == canonical_top and pdir in ("input", "inout"):
                    add_conn(f"top.{top_port}", f"{inst}.{pname}")


    # 2) peer-to-peer exact/normalized-name matching
    producers = defaultdict(list)
    consumers = defaultdict(list)

    for inst, ports in inst_ports.items():
        for p in ports:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "").split("[", 1)[0].strip()
            norm = _normalize_port_name(pname)
            pdir = str(p.get("direction") or p.get("dir") or "").lower().strip()
            width = _port_width(p)
            item = {"inst": inst, "port": pname, "norm": norm, "width": width}

            if pdir in ("output", "inout"):
                producers[norm].append(item)
            if pdir in ("input", "inout"):
                consumers[norm].append(item)

   
    for norm_name, srcs in producers.items():
        candidate_dsts = []
        # first try exact normalized-name consumers
        exact_dsts = consumers.get(norm_name, [])
        if exact_dsts:
            candidate_dsts.extend(exact_dsts)
        else:
            # only fall back to semantic grouping when exact matching found nothing
            s_group = _semantic_group(norm_name)
            for c_norm, c_dsts in consumers.items():
                if c_norm == norm_name:
                    continue
                if _semantic_group(c_norm) == s_group:
                    candidate_dsts.extend(c_dsts)

        if not candidate_dsts:
            continue

        # de-dup candidate destinations
        dedup = []
        seen_d = set()
        for d in candidate_dsts:
            key = (d["inst"], d["port"])
            if key in seen_d:
                continue
            seen_d.add(key)
            dedup.append(d)
        dsts = dedup

        for s in srcs:
            compatible = []
            for d in dsts:
                if s["inst"] == d["inst"]:
                   continue
                if s["width"] is not None and d["width"] is not None and s["width"] != d["width"]:
                   continue
                compatible.append(d)

            exact = [d for d in compatible if d["port"] == s["port"]]
            chosen = exact if len(exact) == 1 else compatible if len(compatible) == 1 else []

            for d in chosen:
                add_conn(f'{s["inst"]}.{s["port"]}', f'{d["inst"]}.{d["port"]}')

    
    return connections

def _now() -> str:
    return datetime.datetime.now().isoformat()


def _clean_llm_output_to_json_text(raw: str) -> str:
    if not raw:
        return ""
    raw = (
        raw.replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

    # Keep only first JSON object or array.
    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return raw[first_obj:last_obj + 1].strip()

    first_arr = raw.find("[")
    last_arr = raw.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return raw[first_arr:last_arr + 1].strip()

    return raw.strip()

def _parse_ep(ep: str):
    if not ep or "." not in ep:
        return None, None
    inst, port = ep.split(".", 1)
    return inst.strip(), port.split("[", 1)[0].strip()

def _parse_ep(ep: str):
    if not ep or "." not in ep:
        return None, None
    inst, port = ep.split(".", 1)
    return inst.strip(), port.split("[", 1)[0].strip()


def _coerce_endpoint_token(ep):
    """
    Accept:
      "u_digital.clk"
      {"instance":"u_digital","port":"clk"}
      {"top":"clk"}
      {"top_port":"clk"}
      {"module":"top","port":"clk"}
      {"inst":"u_digital","name":"clk"}
    Return:
      canonical "inst.port" string or None
    """
    if isinstance(ep, str):
        ep = ep.strip()
        return ep if "." in ep else None

    if not isinstance(ep, dict):
        return None

    if ep.get("top_port"):
        return f'top.{str(ep["top_port"]).strip()}'

    if ep.get("top"):
        return f'top.{str(ep["top"]).strip()}'

    inst = ep.get("instance") or ep.get("inst") or ep.get("module")
    port = ep.get("port") or ep.get("name") or ep.get("signal")

    if inst and str(inst).strip().lower() == "top" and port:
        return f'top.{str(port).strip()}'

    if inst and port:
        return f'{str(inst).strip()}.{str(port).strip()}'

    return None


def _port_dir_from_sigs(sig_db: dict, module_name: str, port_name: str):
    if not isinstance(sig_db, dict) or not module_name or not port_name:
        return None

    # shape 1: { "<module>": { "ports": [...] } }
    if module_name in sig_db and isinstance(sig_db[module_name], dict):
        ports = sig_db[module_name].get("ports") or sig_db[module_name].get("interface") or []
        for p in ports:
            if isinstance(p, dict) and p.get("name") == port_name:
                d = _normalize_dir_token(p.get("direction") or p.get("dir") or "")
                return d


    # shape 2: { "modules": { "<module>": { "ports": [...] } } }
    mods = sig_db.get("modules")
    if isinstance(mods, dict) and module_name in mods and isinstance(mods[module_name], dict):
        ports = mods[module_name].get("ports") or mods[module_name].get("interface") or []
        for p in ports:
            if isinstance(p, dict) and p.get("name") == port_name:
                d = str(p.get("direction") or p.get("dir") or "").lower().strip()
                return d or None

    return None


def _resolve_port_dir(inst_name: str, port_name: str, inst2mod: dict, digital_sigs: dict, analog_sigs: dict):
    if inst_name == "top":
        return "top"
    mod = inst2mod.get(inst_name)
    if not mod:
        return None
    return (
        _port_dir_from_sigs(digital_sigs, mod, port_name)
        or _port_dir_from_sigs(analog_sigs, mod, port_name)
    )


def _sanitize_connections(intent: dict, digital_sigs: dict, analog_sigs: dict):
    """
    Keep only plausible connections:
      - instance(output) -> instance(input)
      - top -> instance(input)
      - instance(output) -> top
    Drop:
      - instance(input) -> top
      - top -> instance(output)
      - input->input, output->output when known
    If direction cannot be determined, keep the edge rather than over-prune.
    """
    inst2mod = _instance_to_module(intent)
    cleaned = []

    for c in intent.get("connections", []):
        if not isinstance(c, dict):
            continue

        src = c.get("from")
        dst = c.get("to")
        if not src or not dst:
            continue

        si, sp = _parse_ep(src)
        di, dp = _parse_ep(dst)
        if not si or not sp or not di or not dp:
            continue

        sdir = _resolve_port_dir(si, sp, inst2mod, digital_sigs, analog_sigs)
        ddir = _resolve_port_dir(di, dp, inst2mod, digital_sigs, analog_sigs)

        # Case 1: top -> instance(input) is valid
        if si == "top" and di != "top":
            if ddir is None or ddir == "input" or ddir == "inout":
                cleaned.append(c)
            continue

        

        # Case 2: instance(output) -> top is valid

        if si != "top" and di == "top":
            if sdir in ("output", "inout"):
                cleaned.append(c)
            continue
        

        # Case 3: top -> top is meaningless
        if si == "top" and di == "top":
            continue

        # Case 4: instance -> instance
        if sdir and ddir:
            if sdir in ("output", "inout") and ddir in ("input", "inout"):
                cleaned.append(c)
            continue

        # If one or both directions are unknown, keep it for now.
        cleaned.append(c)

    intent["connections"] = cleaned
    return intent


def _instance_to_module(intent: dict):
    out = {}
    for inst in intent.get("instances", []):
        if isinstance(inst, dict) and inst.get("name") and inst.get("module"):
            out[inst["name"]] = inst["module"]
    return out

def _pick_primary_module_name(sig_db: dict, fallback: str) -> str:
    if not isinstance(sig_db, dict):
        return fallback

        # direct module dict shape
    for k, v in sig_db.items():
        if isinstance(v, dict) and ("ports" in v or "interface" in v):
            return str(k).strip()

        # nested modules shape
    mods = sig_db.get("modules")
    if isinstance(mods, dict) and mods:
        first = next(iter(mods.keys()))
        return str(first).strip()

    return fallback

  

def run_agent(state: dict) -> dict:
    agent_name = "System Integration Intent Agent"
    print("\n🧠 Running System Integration Intent Agent (SoC integration manifest).")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Inputs (accept a few common keys to reduce friction)

    integration_description = (
        state.get("system_integration_description")
        or state.get("soc_integration_description")
        or state.get("integration_description")
        or state.get("soc_integration_spec_text")
        or state.get("soc_integration_spec")
        or state.get("soc_spec")
        or state.get("system_spec")
        or state.get("description")
        or ""
    ).strip()


    if not integration_description:
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system/integration",
                filename="intent_agent_state_keys.txt",
                content="\n".join(sorted(state.keys())),
            )
        except Exception:
            pass
        state["status"] = "❌ No system_integration_description provided."
        return state

    # Top module base name (we will produce *_sim and *_phys)
    top_base = (state.get("top_module") or state.get("soc_top_name") or "soc_top").strip()

    # Signatures (best-effort)
    # digital/analog signature agents may store differently; accept multiple keys.

    digital_sigs = (
        state.get("digital_module_signature")
        or state.get("digital_rtl_signatures")
        or state.get("rtl_signatures")
        or {}
    )

    

    if not digital_sigs:
        for root, _, files in os.walk(workflow_dir):
            if "rtl_signatures.json" in files:
                p = os.path.join(root, "rtl_signatures.json")
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        digital_sigs = json.load(f)
                        break
                except Exception:
                    pass

    # Generic RTL fallback if signature JSON is absent

    # Broad subtree scan fallback if signature JSON is absent
    if not digital_sigs:
        digital_sigs = _discover_signatures_under(workflow_dir, "digital")

    analog_sigs = (
        state.get("analog_module_signature")
        or state.get("analog_rtl_signatures")
        or state.get("analog_signatures")
        or {}
    )

    if not analog_sigs:
        analog_sigs = _discover_signatures_under(workflow_dir, "analog")

    if not digital_sigs or not analog_sigs:
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system/integration",
                filename="system_integration_intent_debug.json",
                content=json.dumps({
                    "reason": "signature discovery failed",
                    "digital_sigs": digital_sigs,
                    "analog_sigs": analog_sigs,
                    "workflow_dir": workflow_dir,
                }, indent=2),
            )
        except Exception:
            pass

        state["status"] = "❌ Signature discovery failed for system integration"
        return state
    

    # Optional hints from upstream analog agents
    # If your analog behavioral model and macro stub use different module names, provide them here.
    analog_behavioral_module = (state.get("analog_behavioral_module") or "").strip()  # e.g., "adc_model"
    analog_macro_module = (state.get("analog_macro_module") or "").strip()            # e.g., "adc_macro"

    if not integration_description:
        state["status"] = "❌ No system_integration_description provided."
        return state

    # -----------------------------------------------------------------
    # Strict JSON schema for downstream assembly.
    # Includes variant module overrides so we can generate sim/phys tops.
    # -----------------------------------------------------------------
    digital_module = (
        state.get("digital_top_module")
        or state.get("digital_module_name")
        or _pick_primary_module_name(digital_sigs, "digital_block")
    ).strip()

    analog_phys_module = (
        analog_macro_module
        or state.get("analog_phys_module")
        or state.get("analog_macro_name")
        or state.get("analog_top_module")
        or state.get("analog_module_name")
        or _pick_primary_module_name(analog_sigs, "analog_block")
    ).strip()

    analog_phys_module = (
        analog_macro_module
        or state.get("analog_top_module")
        or state.get("analog_module_name")
        or _pick_primary_module_name(analog_sigs, "analog_block")
    ).strip()


    analog_sim_module = (
       analog_behavioral_module
       or analog_phys_module
    ).strip()

    if analog_sim_module == analog_phys_module and analog_macro_module:
        analog_phys_module = analog_macro_module

    schema = {
        "top": {
            "base_name": top_base,
            "sim_module": f"{top_base}_sim",
            "phys_module": f"{top_base}_phys",
            "notes": "Generic system integration manifest for digital + analog assembly."
        },
        "instances": [
            {"name": "u_digital", "module": digital_module},
            {"name": "u_analog", "module": analog_phys_module}
        ],
        "connections": [],
        "tieoffs": [],
        "variants": {
            "sim": {
                "module_overrides": {
                    "u_analog": analog_sim_module
                }
            },
            "phys": {
                "module_overrides": {
                    "u_analog": analog_phys_module
                }
            }
        }
        "control_model": {},
        "observable_behaviors": [],
        "test_intent": {},
        "ownership": {},
        "notes": []
    }

    # Provide module name hints if user supplied them
    # (this improves correctness; still keeps LLM flexibility)
    variant_hint = ""
    if analog_behavioral_module or analog_macro_module:
        variant_hint = f"""
ANALOG MODULE NAME HINTS:
- analog_behavioral_module: {analog_behavioral_module or "(not provided)"}
- analog_macro_module: {analog_macro_module or "(not provided)"}
Use these in variants.module_overrides for instance 'u_analog' if applicable

""".strip()


prompt = f"""
SYSTEM / SoC INTEGRATION DESCRIPTION:
{integration_description}

TOP BASE NAME:
{top_base}

DIGITAL RTL SIGNATURES (modules + ports):
{json.dumps(digital_sigs, indent=2)}

ANALOG RTL SIGNATURES (modules + ports, if available):
{json.dumps(analog_sigs, indent=2)}

{variant_hint}

---
You are a professional SoC integration engineer.

CONTEXT / CONSTRAINTS:
- Generate a generic system integration manifest for the blocks described in the provided signatures and integration description.
- Prefer a minimal top with the fewest required instances and explicit point-to-point connections.
- Keep sim/phys top port intent consistent.
- Do NOT assume any specific protocol (APB/I2C/etc.) unless explicitly present in inputs.
- Do NOT hardcode signal names. Everything must be derived from the datasheet and signatures.

STRICT CONNECTIVITY RULES:
- Respect actual port directions from discovered module signatures.
- Never connect top.<signal> -> instance.<port> if instance.<port> is an output.
- Never connect instance.<port> -> top.<signal> if instance.<port> is an input.
- For instance-to-instance edges, source must be output/inout and destination must be input/inout.
- Do not invent grouped buses or proxy nets.

---

🧠 SYSTEM-LEVEL SEMANTIC REQUIREMENTS (NEW)

In addition to connectivity, you MUST describe system-level behavior using ONLY information derived from the datasheet:

1) TOP-LEVEL CONTRACT (under "top"):
   - functionality: What the integrated system does (high-level)
   - responsibilities: What the top must ensure
   - must_drive / must_receive / must_not_drive
   - reset_behavior: How system behaves during reset
   - behavior_rules: Constraints that must always hold

2) CONTROL MODEL (do NOT hardcode protocol):
   - Infer how the system is controlled (register-driven, transaction-driven, pin-driven, etc.)
   - If a register map exists, reference it but DO NOT duplicate it

3) OBSERVABLE BEHAVIORS:
   - 2–6 behaviors visible at system level
   - Derived from integration + datasheet
   - Examples (do NOT copy literally):
     control → action → response
     reset → idle → activation

4) TEST INTENT:
   - smoke_sequences: basic bring-up scenarios derived from design
   - negative_sequences: illegal/edge cases (reset, invalid ordering)
   - coverage_focus: what system-level coverage should observe

5) OWNERSHIP:
   - Identify which instance owns each top-level observable output
   - Identify which instance drives control-path signals

---

🔒 IMPORTANT OUTPUT FORMAT RULES
- DO NOT use markdown code fences
- DO NOT include explanations
- ONLY output valid JSON
- JSON must be parseable by json.loads

- Use EXACT schema keys:

  top,
  instances,
  connections,
  tieoffs,
  variants,
  control_model,
  observable_behaviors,
  test_intent,
  ownership,
  notes

- DO NOT hardcode signal names or protocols
- ALL behavioral fields must be derived from the provided description/signatures

---

TARGET JSON SCHEMA EXAMPLE:
{json.dumps(schema, indent=2)}

Now output JSON only.
""".strip()



    raw = llm_text(prompt)
    cleaned = _clean_llm_output_to_json_text(raw)

    if not cleaned:
        state["status"] = "❌ LLM returned empty output for system integration intent."
        return state

    try:

        intent = {}
        parse_error = None

        try:
            intent = safe_json_load(cleaned) if cleaned else {}
        except Exception as pe:
            parse_error = pe
            intent = {}

        if not isinstance(intent, dict) or not intent:
            intent = _fallback_intent_from_raw_text(
                raw=raw,
                top_base=top_base,
                digital_module=digital_module,
                analog_phys_module=analog_phys_module,
                schema=schema,
            )

        # Seed defaults early so normalization/rescue always has structure to work with
        intent.setdefault("top", {})
        intent.setdefault("instances", [])
        intent.setdefault("connections", [])
        intent.setdefault("tieoffs", [])
        intent.setdefault("variants", {})

        if isinstance(intent["top"], dict):
            intent["top"].setdefault("base_name", top_base)
            intent["top"].setdefault("sim_module", f"{top_base}_sim")
            intent["top"].setdefault("phys_module", f"{top_base}_phys")

        if not intent["instances"]:
            intent["instances"] = [
                {"name": "u_digital", "module": digital_module},
                {"name": "u_analog", "module": analog_phys_module},
            ]

        if "sim" not in intent["variants"]:
            intent["variants"]["sim"] = schema["variants"]["sim"]
        if "phys" not in intent["variants"]:
            intent["variants"]["phys"] = schema["variants"]["phys"]

        # NEW: convert net-style connections into canonical from/to edges
        intent = _normalize_connection_entries(intent, digital_sigs, analog_sigs)

        # Then sanitize canonical edges
        intent = _sanitize_connections(intent, digital_sigs, analog_sigs)

        # --- validate ports actually exist ---
        inst2mod = _instance_to_module(intent)

        valid_connections = []

        for c in intent.get("connections", []):
            src = c.get("from")
            dst = c.get("to")

            si, sp = _parse_ep(src)
            di, dp = _parse_ep(dst)

            if not si or not sp or not di or not dp:
                continue

            if si != "top":
                mod = inst2mod.get(si)
                ports = _collect_ports_for_module(digital_sigs, mod) or _collect_ports_for_module(analog_sigs, mod)
                if sp not in [p.get("name") for p in ports if isinstance(p, dict)]:
                    continue

            if di != "top":
                mod = inst2mod.get(di)
                ports = _collect_ports_for_module(digital_sigs, mod) or _collect_ports_for_module(analog_sigs, mod)
                if dp not in [p.get("name") for p in ports if isinstance(p, dict)]:
                    continue

            valid_connections.append(c)

        intent["connections"] = valid_connections



        # Backward-compatible recovery:
        # run if usable connectivity is still too weak for top assembly.
        usable_connections = intent.get("connections", []) or []
        usable_tieoffs = intent.get("tieoffs", []) or []

        needs_recovery = (
            len(usable_connections) < 2
            or not any(str(c.get("from", "")).startswith("top.") or str(c.get("to", "")).startswith("top.") for c in usable_connections)
        )

        if needs_recovery:
            rescue_connections, rescue_top_ports = _build_deterministic_rescue_connections(intent, digital_sigs, analog_sigs)
            if rescue_connections:
                intent["connections"] = rescue_connections
                intent.setdefault("notes", [])
                if isinstance(intent["notes"], list):
                    intent["notes"].append(
                        "Applied deterministic rescue connections to guarantee integrated SoC top."
                )
            else:
                fallback_connections = _build_generic_fallback_connections(intent, digital_sigs, analog_sigs)
                if fallback_connections:
                    intent["connections"] = fallback_connections
                    intent.setdefault("notes", [])
                    if isinstance(intent["notes"], list):
                        intent["notes"].append(
                            "Applied generic fallback connections after deterministic rescue found no valid mapping."
                        )

        
                
        
                
    except Exception as e:
        # Save raw output for debugging
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system/integration",
                filename="system_integration_intent_raw.txt",
                content=raw,
            )
        except Exception:
            pass
        state["status"] = f"❌ Failed to parse system integration intent JSON: {e}"
        return state

    if not isinstance(intent, dict):
        intent = {}


    if not intent.get("connections") and not intent.get("tieoffs"):
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system/integration",
                filename="system_integration_intent_debug.json",
                content=json.dumps({
                    "digital_sigs": digital_sigs,
                    "analog_sigs": analog_sigs,
                    "intent_after_sanitize": intent,
                }, indent=2),
            )
        except Exception:
            pass

        state["status"] = "❌ System integration intent has no connections/tieoffs after sanitize + generic fallback."
        state["system_integration_intent"] = intent
        return state

    intent_json = json.dumps(intent, indent=2)

    local_intent_abs = os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json")
    os.makedirs(os.path.dirname(local_intent_abs), exist_ok=True)
    with open(local_intent_abs, "w", encoding="utf-8") as f:
        f.write(intent_json)

    # Persist artifact
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="system/integration",
            filename="system_integration_intent.json",
            content=json.dumps(intent, indent=2),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload system integration intent artifact: {e}")

    
    print("DEBUG intent agent file:", __file__)
    print("DEBUG intent keys:", list(intent.keys()))
    print("DEBUG intent instances:", intent.get("instances"))
    print("DEBUG intent connections:", intent.get("connections"))
    print("DEBUG intent tieoffs:", intent.get("tieoffs"))
    state["system_integration_intent"] = intent

    state["status"] = "✅ System integration intent generated"
    return state
