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
    if norm_name in {"clk", "clock", "sysclk", "coreclk", "pclk", "aclk"}:
        return "clock"
    if norm_name in {"rst", "reset", "rstn", "resetn", "aresetn", "presetn"}:
        return "reset"
    if norm_name in {"vdd", "vss", "gnd", "vcc", "vin", "vref", "avdd", "dvdd", "avss", "dvss"}:
        return "power"
    return None


def _get_instance_ports(intent: dict, inst_name: str, digital_sigs: dict, analog_sigs: dict):
    inst2mod = _instance_to_module(intent)
    mod = inst2mod.get(inst_name)
    if not mod:
        return []
    return _collect_ports_for_module(digital_sigs, mod) or _collect_ports_for_module(analog_sigs, mod)


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
                top_candidates.add(pname)

    for top_port in sorted(top_candidates):
        for inst, ports in inst_ports.items():
            for p in ports:
                if not isinstance(p, dict):
                    continue
                pname = str(p.get("name") or "").split("[", 1)[0].strip()
                pdir = str(p.get("direction") or p.get("dir") or "").lower().strip()
                if pname == top_port and pdir in ("input", "inout"):
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
        dsts = consumers.get(norm_name, [])
        if not dsts:
            continue

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


def _port_dir_from_sigs(sig_db: dict, module_name: str, port_name: str):
    if not isinstance(sig_db, dict) or not module_name or not port_name:
        return None

    # shape 1: { "<module>": { "ports": [...] } }
    if module_name in sig_db and isinstance(sig_db[module_name], dict):
        ports = sig_db[module_name].get("ports") or sig_db[module_name].get("interface") or []
        for p in ports:
            if isinstance(p, dict) and p.get("name") == port_name:
                d = str(p.get("direction") or p.get("dir") or "").lower().strip()
                return d or None

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

    digital_sigs = state.get("digital_rtl_signatures") or state.get("rtl_signatures") or {}

    if not digital_sigs:
        for root, _, files in os.walk(workflow_dir):
            if "rtl_signatures.json" in files:
                p = os.path.join(root, "rtl_signatures.json")
                try:
                    with open(p, "r", encoding="utf-8") as f:
                       digital_sigs = json.load(f)
                except Exception:
                    pass
                    
    analog_sigs = state.get("analog_rtl_signatures") or state.get("analog_signatures") or {}

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
        or state.get("analog_top_module")
        or state.get("analog_module_name")
        or _pick_primary_module_name(analog_sigs, "analog_block")
    ).strip()


    analog_sim_module = (
       analog_behavioral_module
       or analog_phys_module
    ).strip()

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
- Keep sim/phys top port intent consistent. If analog sim/phys module names differ, use variants.module_overrides.
- Reuse identical port names across blocks whenever appropriate.
- Do not assume ADC-specific names unless they are present in the input signatures/description.


🔒 IMPORTANT OUTPUT FORMAT RULES
- DO NOT use markdown code fences (no ```json, no ```).
- DO NOT include explanations or extra text.
- ONLY output raw JSON.
- JSON must be 100% valid (parseable by json.loads).
- Use the schema exactly: top, instances, connections, tieoffs, variants.
- 'connections' must connect valid ports that exist in the provided signatures whenever possible.

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

        intent = safe_json_load(cleaned) if cleaned else {}
        if not isinstance(intent, dict):
            intent = {}

        intent = _sanitize_connections(intent, digital_sigs, analog_sigs)

        # Backward-compatible generic recovery:
        # only infer connections if sanitize removed everything.
        if not intent.get("connections") and not intent.get("tieoffs"):
            fallback_connections = _build_generic_fallback_connections(intent, digital_sigs, analog_sigs)
            if fallback_connections:
                intent["connections"] = fallback_connections
                intent.setdefault("notes", [])
                if isinstance(intent["notes"], list):
                    intent["notes"].append(
                        "Applied generic fallback connections after direction validation removed all candidate edges."
                    )
            else:
                intent.setdefault("notes", [])
                if isinstance(intent["notes"], list):
                    intent["notes"].append(
                        "All candidate connections were removed by direction validation and no unambiguous generic fallback connections could be inferred."
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

    intent.setdefault("top", {})
    intent.setdefault("instances", [])
    intent.setdefault("connections", [])
    intent.setdefault("tieoffs", [])
    intent.setdefault("variants", {})

    if isinstance(intent["top"], dict):
        intent["top"].setdefault("base_name", top_base)
        intent["top"].setdefault("sim_module", f"{top_base}_sim")
        intent["top"].setdefault("phys_module", f"{top_base}_phys")

    # Generic fallback if LLM under-specifies the manifest
    if not intent["instances"]:
        intent["instances"] = schema["instances"]

    if "sim" not in intent["variants"]:
        intent["variants"]["sim"] = schema["variants"]["sim"]
    if "phys" not in intent["variants"]:
        intent["variants"]["phys"] = schema["variants"]["phys"]

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
