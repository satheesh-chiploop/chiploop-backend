import os
import re
import json
import datetime
import subprocess
import logging
logger = logging.getLogger("chiploop")
from typing import Dict, List, Tuple, Optional

from portkey_ai import Portkey

from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")


def _safe_json(obj):
    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return json.dumps(str(obj), indent=2)


def _load_json_if_path(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        with open(v, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _normalize_spec_json(spec_json: dict) -> Tuple[dict, str]:
    if not isinstance(spec_json, dict):
        raise ValueError("Spec JSON must be a dictionary.")

    if isinstance(spec_json.get("hierarchy"), dict):
        hier = spec_json["hierarchy"]
        top = hier.get("top_module")
        modules = hier.get("modules", [])

        if not isinstance(top, dict):
            raise ValueError("hierarchy.top_module must be an object.")
        if not top.get("name"):
            raise ValueError("hierarchy.top_module.name is required.")
        if not top.get("rtl_output_file"):
            raise ValueError("hierarchy.top_module.rtl_output_file is required.")
        if not isinstance(modules, list):
            raise ValueError("hierarchy.modules must be a list.")

        return {
            "design_name": spec_json.get("design_name") or top["name"],
            "hierarchy": {
                "top_module": top,
                "modules": modules,
            },
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "top_level_connections": spec_json.get("top_level_connections", []),
            "inter_module_signals": spec_json.get("inter_module_signals", []),
            "signal_ownership": spec_json.get("signal_ownership", []),
            "register_contract": spec_json.get("register_contract", {}),
        }, "hierarchical"

    if spec_json.get("name") and spec_json.get("rtl_output_file"):
        return {
            "name": spec_json["name"],
            "description": spec_json.get("description", ""),
            "ports": spec_json.get("ports", []),
            "functionality": spec_json.get("functionality", ""),
            "responsibilities": spec_json.get("responsibilities", []),
            "must_drive": spec_json.get("must_drive", []),
            "must_receive": spec_json.get("must_receive", []),
            "must_not_drive": spec_json.get("must_not_drive", []),
            "reset_behavior": spec_json.get("reset_behavior", ""),
            "behavior_rules": spec_json.get("behavior_rules", []),
            "operating_constraints": spec_json.get("operating_constraints", {}),
            "rtl_output_file": spec_json["rtl_output_file"],
        }, "flat"

    raise ValueError("Spec JSON must be either flat or hierarchical.")


def _collect_expected_modules(spec_json: dict, mode: str) -> List[dict]:
    if mode == "flat":
        return [spec_json]
    return [spec_json["hierarchy"]["top_module"]] + list(spec_json["hierarchy"].get("modules", []))


def _collect_expected_rtl_files(spec_json: dict, mode: str) -> List[str]:
    return [m["rtl_output_file"] for m in _collect_expected_modules(spec_json, mode)]


def _top_module_name(spec_json: dict, mode: str) -> str:
    return spec_json["name"] if mode == "flat" else spec_json["hierarchy"]["top_module"]["name"]


def _top_rtl_file(spec_json: dict, mode: str) -> str:
    return spec_json["rtl_output_file"] if mode == "flat" else spec_json["hierarchy"]["top_module"]["rtl_output_file"]


def _parse_named_verilog_blocks(llm_output: str) -> Dict[str, str]:
    blocks = re.findall(
        r"---BEGIN\s+([A-Za-z_][\w\-]*\.v)---(.*?)---END\s+\1---",
        llm_output,
        re.DOTALL,
    )
    return {fname.strip(): code.strip() for fname, code in blocks}


def _extract_module_ports(verilog_text: str) -> Dict[str, List[str]]:
    out = {}
    mod_pat = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*;", re.DOTALL)
    for m in mod_pat.finditer(verilog_text):
        mod_name = m.group(1)
        raw_ports = m.group(2)
        port_names = []
        for p in raw_ports.split(","):
            token = p.strip()
            token = re.sub(r"\binput\b|\boutput\b|\binout\b|\bwire\b|\breg\b|\blogic\b|\bsigned\b", "", token)
            token = re.sub(r"\[[^\]]+\]", "", token)
            token = token.strip()
            if token:
                parts = token.split()
                if parts:
                    port_names.append(parts[-1].strip())
        out[mod_name] = port_names
    return out


def _normalize_signal_token(name: str) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"\[[^\]]+\]", "", name).strip()


def _split_endpoint(endpoint: str):
    if "." not in endpoint:
        raise ValueError(f"Invalid endpoint format: {endpoint}")
    mod, port = endpoint.split(".", 1)
    return mod.strip(), _normalize_signal_token(port.strip())


def _port_dir_map(module_ports):
    return {p["name"]: p.get("direction") for p in module_ports}


def _build_connectivity_contract(spec_json: dict, mode: str) -> dict:
    if mode != "hierarchical":
        return {
            "mode": mode,
            "modules": {},
            "top_module": _top_module_name(spec_json, mode),
            "top_ports": [],
            "top_level_connections": [],
            "internal_signals": [],
            "ownership": [],
        }

    top = spec_json["hierarchy"]["top_module"]
    modules = [top] + list(spec_json["hierarchy"].get("modules", []))

    module_map = {}
    for m in modules:
        module_map[m["name"]] = {
            "name": m["name"],
            "ports": m.get("ports", [])
        }

    top_ports = [p["name"] for p in top.get("ports", [])]

    internal_signals = []
    for sig in spec_json.get("inter_module_signals", []):
        src_mod, src_port = _split_endpoint(sig["source"])
        dsts = []
        for d in sig.get("destinations", []):
            dm, dp = _split_endpoint(d)
            dsts.append({"module": dm, "port": dp})

        internal_signals.append({
            "name": _normalize_signal_token(sig["name"]),
            "width": sig["width"],
            "source": {"module": src_mod, "port": src_port},
            "destinations": dsts,
            "description": sig.get("description", "")
        })

    top_conns = []
    for c in spec_json.get("top_level_connections", []):
        dsts = []
        for d in c.get("connected_to", []):
            dm, dp = _split_endpoint(d)
            dsts.append({"module": dm, "port": dp})
        top_conns.append({
            "top_port": _normalize_signal_token(c["top_port"]),
            "connected_to": dsts,
            "description": c.get("description", "")
        })

    ownership = []
    for o in spec_json.get("signal_ownership", []):
        om, op = _split_endpoint(o["owner"])
        ownership.append({
            "signal": _normalize_signal_token(o["signal"]),
            "owner": {"module": om, "port": op}
        })

    return {
        "mode": "hierarchical",
        "modules": module_map,
        "top_module": top["name"],
        "top_ports": top_ports,
        "top_level_connections": top_conns,
        "internal_signals": internal_signals,
        "ownership": ownership,
    }


def _validate_connectivity_contract(spec_json: dict, mode: str) -> List[str]:
    issues = []
    if mode != "hierarchical":
        return issues

    contract = _build_connectivity_contract(spec_json, mode)
    modules = contract["modules"]
    top_module = spec_json["hierarchy"]["top_module"]
    top_port_names = {p["name"] for p in top_module.get("ports", [])}

    for mname, m in modules.items():
        if not m["ports"]:
            issues.append(f"❌ Module '{mname}' has empty ports in hierarchical spec.")

    for c in contract["top_level_connections"]:
        if c["top_port"] not in top_port_names:
            issues.append(f"❌ top_level_connections references unknown top port '{c['top_port']}'.")
        for dst in c["connected_to"]:
            if dst["module"] not in modules:
                issues.append(f"❌ top_level_connections target module '{dst['module']}' does not exist.")
                continue
            dirs = _port_dir_map(modules[dst["module"]]["ports"])
            if dst["port"] not in dirs:
                issues.append(f"❌ top_level_connections target port '{dst['module']}.{dst['port']}' does not exist.")

    for sig in contract["internal_signals"]:
        sm = sig["source"]["module"]
        sp = sig["source"]["port"]
        if sm not in modules:
            issues.append(f"❌ inter_module_signals source module '{sm}' does not exist.")
        else:
            dirs = _port_dir_map(modules[sm]["ports"])
            if sp not in dirs:
                issues.append(f"❌ inter_module_signals source port '{sm}.{sp}' does not exist.")

        for dst in sig["destinations"]:
            dm = dst["module"]
            dp = dst["port"]
            if dm not in modules:
                issues.append(f"❌ inter_module_signals destination module '{dm}' does not exist.")
            else:
                dirs = _port_dir_map(modules[dm]["ports"])
                if dp not in dirs:
                    issues.append(f"❌ inter_module_signals destination port '{dm}.{dp}' does not exist.")

    return issues


def _validate_spec_vs_rtl(spec_json: dict, mode: str, verilog_map: Dict[str, str]) -> Tuple[List[str], List[str], List[str]]:
    issues = []
    clock_ports = []
    reset_ports = []

    expected_modules = _collect_expected_modules(spec_json, mode)
    expected_files = set(_collect_expected_rtl_files(spec_json, mode))
    actual_files = set(verilog_map.keys())

    missing_files = sorted(expected_files - actual_files)
    extra_files = sorted(actual_files - expected_files)

    if missing_files:
        issues.append(f"❌ Missing expected RTL files: {missing_files}")
    if extra_files:
        issues.append(f"⚠ Extra RTL files emitted: {extra_files}")

    for mod in expected_modules:
        mod_name = mod["name"]
        rtl_file = mod["rtl_output_file"]
        spec_ports = [p["name"] for p in mod.get("ports", [])]

        code = verilog_map.get(rtl_file)
        if not code:
            continue

        extracted = _extract_module_ports(code)
        if mod_name not in extracted:
            issues.append(f"❌ Module '{mod_name}' not found in file '{rtl_file}'.")
            continue

        rtl_ports = extracted[mod_name]
        missing_ports = [p for p in spec_ports if p not in rtl_ports]
        extra_ports2 = [p for p in rtl_ports if p not in spec_ports]

        if missing_ports:
            issues.append(f"❌ Module '{mod_name}' missing ports vs spec: {missing_ports}")
        if extra_ports2:
            issues.append(f"❌ Module '{mod_name}' has extra ports vs spec: {extra_ports2}")

        for p in mod.get("ports", []):
            pname = p["name"]
            if re.search(r"clk|clock", pname, re.IGNORECASE):
                clock_ports.append(pname)
            if re.search(r"rst|reset", pname, re.IGNORECASE):
                reset_ports.append(pname)

    full_text = "\n".join(verilog_map.values())

    if mode == "hierarchical":
        contract = _build_connectivity_contract(spec_json, mode)

        for c in contract["top_level_connections"]:
            tp = c["top_port"]
            if tp and tp not in full_text:
                issues.append(f"⚠ Top-level connection signal '{tp}' not clearly visible in RTL text.")

        for s in contract["internal_signals"]:
            sig_name = s["name"]
            if sig_name and sig_name not in full_text:
                issues.append(f"❌ Inter-module signal '{sig_name}' not found in RTL.")

        for o in contract["ownership"]:
            sig = o["signal"]
            owner = f"{o['owner']['module']}.{o['owner']['port']}"
            if sig and sig not in full_text:
                issues.append(f"⚠ Owned signal '{sig}' from '{owner}' not found in RTL.")

    return issues, sorted(set(clock_ports)), sorted(set(reset_ports))


def _build_generation_prompt(spec_json: dict, mode: str, regmap_obj: Optional[dict], clock_reset_obj: Optional[dict], power_intent_obj: Optional[dict]) -> str:
    connectivity_contract = _build_connectivity_contract(spec_json, mode)

    return f"""
You are a senior ASIC RTL engineer.

The input DIGITAL_SPEC_JSON is the single source of truth.
You must implement it exactly.
Do NOT redesign architecture.
Do NOT rename modules.
Do NOT rename ports.
Do NOT change rtl_output_file names.
Do NOT add extra modules.
Do NOT drop required modules.
Do NOT add extra ports.
Do NOT omit required ports.

STRICT OUTPUT RULES
- Output ONLY named Verilog file blocks.
- No markdown fences.
- No explanations.
- Use this exact format:
---BEGIN file_name.v---
<verilog here>
---END file_name.v---

SPEC MODE:
{mode}

DIGITAL_SPEC_JSON:
{_safe_json(spec_json)}

DERIVED_INTERFACE_CONTRACT:
{_safe_json(connectivity_contract)}

DIGITAL_REGMAP_JSON:
{_safe_json(regmap_obj) if regmap_obj is not None else "null"}

CLOCK_RESET_ARCH_JSON:
{_safe_json(clock_reset_obj) if clock_reset_obj is not None else "null"}

POWER_INTENT_JSON:
{_safe_json(power_intent_obj) if power_intent_obj is not None else "null"}

IMPLEMENTATION RULES

- Generate synthesizable Verilog-2005 only.
- Do NOT use SystemVerilog constructs.
- Forbidden constructs include:
  - typedef
  - enum
  - logic
  - always_comb
  - always_ff
  - struct
  - union
  - packed arrays
  - unpacked array ports
  - unique case
  - priority case
- Use only Verilog-2005 constructs such as:
  - module
  - input/output/inout
  - wire
  - reg
  - localparam
  - assign
  - always @(*)
  - always @(posedge clk or negedge rst_n)
- If SPEC MODE is flat, generate exactly one module file only.
- If SPEC MODE is hierarchical, generate every required module file from spec.
- Each file must contain the module declared in its rtl_output_file mapping.
- All module headers must exactly match the spec contract.
- Use only declared signals.
- No undeclared identifiers.
- No TODOs.
- No empty shells.
- Drive all outputs.
- Use DIGITAL_SPEC_JSON module functionality, responsibilities, must_drive, must_receive, must_not_drive, reset_behavior, and behavior_rules as hard requirements.
- Use DERIVED_INTERFACE_CONTRACT as the exact wiring contract.
- For each top-level connection, connect the declared top port to the listed module ports.
- For each internal signal, create exactly one internal wire of the declared width.
- Drive that wire only from the declared source endpoint.
- Consume that wire only at the declared destination endpoints.
- Respect ownership exactly; do not invent alternate drivers or alternate buses.
- If a top-level output is owned by a submodule according to signal_ownership, the top module must expose it only through structural wiring/port connections.
- The top module must NOT procedurally assign or reset a top-level output that is owned by a submodule.
- Do NOT add top-level always blocks that drive outputs already driven by child modules.
- Do not collapse multiple declared signals into a grouped convenience bus unless the spec explicitly defines that bus.
- If multiple scalar/vector signals are declared separately in module ports, connect them separately by name.
- Do NOT invent aggregate ports such as reg_bus, reg_bus_signals, ctrl_bus, status_bus, or similar unless explicitly present in DIGITAL_SPEC_JSON.
- If there is a register map, implement real stored writable registers where required.
- Implement STATUS and INT_STATUS from explicit field semantics if regmap provides them.
- If a wider value is split across multiple narrower registers, reconstruct it to the exact declared signal width only.
- Example rule: if a 12-bit signal uses one low byte and one high nibble, reconstruct as {{high_reg[3:0], low_reg[7:0]}}, not as a 16-bit concatenation.
- When reconstructing a wider signal from register bytes, the concatenation width must exactly match the declared destination width.
- If cfg_dac_code is [11:0], reconstruct only 12 bits, for example:
  {{dac_code_h[3:0], dac_code_l[7:0]}}
- Never concatenate two full 8-bit registers into a 12-bit destination.
- Never assign a concatenation wider than the declared destination signal width.
- Prefer the simplest deterministic smoke-test implementation consistent with the contract.
- If any module uses an FSM, implement states using Verilog-2005 localparam constants and reg state registers.
- Do NOT use typedef enum or any SystemVerilog FSM syntax.
- Entire design must compile together cleanly.
- A module must NEVER reference another module instance by hierarchical name.
- Forbidden examples inside child RTL:
  - interrupt_ctrl.irq
  - u_interrupt_ctrl.irq
  - digital_subsystem.some_wire
- If a register block needs interrupt status, that status must arrive through an explicit declared input port from the top-level wiring contract.
- Every child module must be self-contained and may only use:
  - its own ports
  - its own local regs/wires/params
- Distinguish RW storage registers from RO view registers.
- RW registers must be backed by explicit stored regs when written by software.
- RO registers must NOT invent undeclared storage elements just because the regmap gives them names.
- If a RO register represents fields from an input/status signal, implement readback directly from the corresponding declared input port or from an explicitly declared shadow/status reg.
- Example: if the regmap contains ADC_DATA_L and ADC_DATA_H and the module has input adc_data_sync[11:0], then:
  - ADC_DATA_L readback must come from adc_data_sync[7:0]
  - ADC_DATA_H readback must come from the upper nibble packed into 8 bits, e.g. {{4'b0000, adc_data_sync[11:8]}}
- Do NOT reference symbolic register names such as ADC_DATA_L or ADC_DATA_H in RTL unless you explicitly declared them as reg/wire objects in that module.
- Every identifier used in a read-data mux must be either:
  - a declared reg
  - a declared wire
  - a declared port
  - a literal/concatenation/slice of declared signals

SELF-CHECK BEFORE OUTPUT
1. Every expected file is emitted exactly once.
2. Every module name matches spec.
3. Every port list matches spec exactly.
4. No missing or extra ports.
5. No undeclared signals.
6. No width mismatches.
7. top_level_connections are reflected in the top RTL.
8. inter_module_signals are reflected as actual internal wires and connections.
9. signal_ownership is respected, with one legal driver per owned signal.
10. Stored registers are not faked by directly echoing bus write data on reads.
11. No SystemVerilog syntax is used.
12. No top-level always block drives an output owned by a child module.
13. FSMs use localparam + reg state encoding only.
14. For every case item in a register read-data mux, the right-hand-side expression must use only declared identifiers.
15. RO registers described in DIGITAL_REGMAP_JSON must map to declared status/input signals or explicitly declared shadow regs; never use undeclared symbolic register names from the regmap.

""".strip()

def _find_fallback_spec_json(workflow_dir: str):
    spec_dir = os.path.join(workflow_dir, "spec")
    if not os.path.isdir(spec_dir):
        return None
    cands = []
    for fn in os.listdir(spec_dir):
        if fn.endswith("_spec.json"):
            cands.append(os.path.join(spec_dir, fn))
    cands.sort()
    return cands[0] if cands else None

def _record_text_artifact_safe(workflow_id, agent_name, subdir, filename, path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir=subdir,
                    filename=filename,
                    content=f.read(),
                )
    except Exception as e:
        print(f"⚠️ Failed to upload artifact {filename}: {e}")

def _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir):
    for fname in [
        "rtl_agent_entry.json",
        "rtl_agent_preflight.json",
        "rtl_agent_compile.log",
        "rtl_agent_summary.txt",
        "rtl_agent_exception.txt",
        "rtl_llm_raw_output.txt",
    ]:
        _record_text_artifact_safe(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="rtl",
            filename=fname,
            path=os.path.join(rtl_dir, fname),
        )


def run_agent(state: dict) -> dict:
    agent_name = "Digital RTL Agent"
    print("\n🧠 Running RTL Agent (implementation mode).")

    def _stage(msg: str):
        logger.info(f"[RTL DEBUG] {msg}")

    _stage("entered_run_agent")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Restore local directory structure
    rtl_dir = os.path.join(workflow_dir, "rtl")
    os.makedirs(rtl_dir, exist_ok=True)

    def _fail_and_upload(msg: str, exc: Exception = None) -> dict:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        error_file = os.path.join(rtl_dir, "rtl_agent_exception.txt")

        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(msg + "\n")
            if exc is not None:
                lf.write(f"Exception type: {type(exc).__name__}\n")
                lf.write(f"Exception: {exc}\n")

        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation failed.\n\n")
            sf.write(msg + "\n")
            if exc is not None:
                sf.write(f"Exception type: {type(exc).__name__}\n")
                sf.write(f"Exception: {exc}\n")

        if exc is not None:
            with open(error_file, "w", encoding="utf-8") as ef:
                ef.write(repr(exc) + "\n")

        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)

        state.update({
            "status": f"❌ RTL generation failed: {msg}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "issues": [msg] + ([str(exc)] if exc is not None else []),
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        return state

    try:
        client_portkey = Portkey(api_key=PORTKEY_API_KEY)
    except Exception as e:
        return _fail_and_upload("Failed to initialize Portkey client.", e)

    entry_log = os.path.join(rtl_dir, "rtl_agent_entry.json")
    with open(entry_log, "w", encoding="utf-8") as ef:
        json.dump({
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
            "digital_spec_json": state.get("digital_spec_json"),
            "spec_json": state.get("spec_json"),
            "digital_spec_json_exists": isinstance(state.get("digital_spec_json"), str) and os.path.exists(state.get("digital_spec_json", "")),
            "spec_json_exists": isinstance(state.get("spec_json"), str) and os.path.exists(state.get("spec_json", "")),
        }, ef, indent=2)

    spec_path = None
    _stage("loading_spec")
    spec_obj = _load_json_if_path(state.get("digital_spec_json"))
    _stage(f"spec_loaded: {spec_obj is not None}")
    if spec_obj is None:
        spec_obj = _load_json_if_path(state.get("spec_json"))
    if spec_obj is None:
        spec_path = _find_fallback_spec_json(workflow_dir)
        _stage("checking_fallback_spec")
        spec_obj = _load_json_if_path(spec_path)

    if not spec_obj:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent could not locate spec JSON.\n")
            lf.write(f"digital_spec_json={state.get('digital_spec_json')}\n")
            lf.write(f"spec_json={state.get('spec_json')}\n")
            lf.write(f"fallback_spec_json={spec_path}\n")
        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation aborted: missing spec JSON.\n")
        state.update({
            "status": "❌ Missing digital spec JSON for RTL generation.",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "issues": ["Missing digital spec JSON for RTL generation."],
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state

    try:
        _stage("normalizing_spec")
        spec_json, mode = _normalize_spec_json(spec_obj)
        _stage(f"normalized_spec: mode={mode}")
    except Exception as e:
        return _fail_and_upload("Spec JSON normalization failed.", e)
    _stage("validating_connectivity")
    pre_issues = _validate_connectivity_contract(spec_json, mode)
    _stage(f"connectivity_valid: {not pre_issues}")
    if pre_issues:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent aborted due to spec connectivity contract violations:\n")
            for issue in pre_issues:
                lf.write(f"{issue}\n")
        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation aborted due to invalid spec connectivity contract.\n\n")
            for issue in pre_issues:
                sf.write(f"{issue}\n")

        state.update({
            "status": "❌ Invalid spec connectivity contract for RTL generation.",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "port_list": [],
            "clock_ports": [],
            "reset_ports": [],
            "issues": pre_issues,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state

    _stage("loading_regmap")

    regmap_obj = (
        _load_json_if_path(state.get("digital_regmap_json"))
        or _load_json_if_path(state.get("digital_regmap"))
    )
    _stage(f"regmap_loaded: {regmap_obj is not None}")

    _stage("loading_clock_reset")

    clock_reset_obj = _load_json_if_path(state.get("clock_reset_arch_path"))

    _stage(f"clock_reset_loaded: {clock_reset_obj is not None}")

    _stage("loading_power_intent")

    power_intent_obj = None
    signoff_obj = state.get("signoff")

    _stage(f"signoff_type: {type(signoff_obj)}")

    if isinstance(signoff_obj, dict):
        pi = signoff_obj.get("power_intent")
        _stage(f"power_intent_type: {type(pi)}")
        if isinstance(pi, dict):
            power_intent_obj = pi

    _stage(f"power_intent_loaded: {power_intent_obj is not None}")


    _stage("building_prompt")
    try:
        prompt = _build_generation_prompt(spec_json, mode, regmap_obj, clock_reset_obj, power_intent_obj)
    except Exception as e:
        logger.exception("[RTL DEBUG] prompt build failed")
        return _fail_and_upload("RTL prompt build failed.", e)
    _stage(f"prompt_length: {len(prompt)}")

    
    _stage("writing_preflight")

    preflight_path = os.path.join(rtl_dir, "rtl_agent_preflight.json")
    with open(preflight_path, "w", encoding="utf-8") as pf:
        json.dump({
            "mode": mode,
            "top_module": _top_module_name(spec_json, mode),
            "expected_files": _collect_expected_rtl_files(spec_json, mode),
            "has_regmap": regmap_obj is not None,
            "has_clock_reset": clock_reset_obj is not None,
            "has_power_intent": power_intent_obj is not None,
            "prompt_chars": len(prompt),
        }, pf, indent=2)
    _stage("preflight_written")

    _stage("starting_llm_call")

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
    except Exception as e:
        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        error_file = os.path.join(rtl_dir, "rtl_agent_exception.txt")

        with open(error_file, "w", encoding="utf-8") as ef:
            ef.write(f"RTL generation exception:\n{repr(e)}\n")

        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("RTL agent failed before RTL materialization.\n")
            lf.write(f"Exception type: {type(e).__name__}\n")
            lf.write(f"Exception: {e}\n")

        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation failed before raw output was written.\n")
            sf.write(f"Exception type: {type(e).__name__}\n")
            sf.write(f"Exception: {e}\n")

        state.update({
            "status": f"❌ RTL generation failed: {e}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": log_path,
            "issues": [f"RTL generation failed: {e}"],
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        return state

    try:
        raw_output_path = os.path.join(rtl_dir, "rtl_llm_raw_output.txt")
        with open(raw_output_path, "w", encoding="utf-8") as f:
            f.write(llm_output)

        verilog_map = _parse_named_verilog_blocks(llm_output)
        if not verilog_map:
            return _fail_and_upload(
                "LLM output did not contain any named Verilog file blocks in the required format."
            )

        expected_files = _collect_expected_rtl_files(spec_json, mode)
        artifact_list = []

        for fname in expected_files:
            code = verilog_map.get(fname)
            if not code:
                continue
            fpath = os.path.join(rtl_dir, fname)
            with open(fpath, "w", encoding="utf-8") as vf:
                vf.write(code + "\n")
            artifact_list.append(fpath)

        issues, clock_ports, reset_ports = _validate_spec_vs_rtl(spec_json, mode, verilog_map)

        forbidden_sv_patterns = [
            r"\btypedef\b",
            r"\benum\b",
            r"\blogic\b",
            r"\balways_comb\b",
            r"\balways_ff\b",
            r"\bstruct\b",
            r"\bunion\b",
        ]
        full_text = "\n".join(verilog_map.values())
        for pat in forbidden_sv_patterns:
            if re.search(pat, full_text):
                issues.append(f"❌ Forbidden SystemVerilog construct found in RTL: pattern '{pat}'")

        suspicious_grouped_buses = [
            "reg_bus_signals",
            "reg_bus",
            "ctrl_bus",
            "status_bus",
        ]
        spec_text = json.dumps(spec_json)
        for name in suspicious_grouped_buses:
            if name in full_text and name not in spec_text:
                issues.append(f"❌ Invented grouped bus '{name}' found in RTL but not declared in spec.")

        top_rtl_file = _top_rtl_file(spec_json, mode)
        top_rtl_path = os.path.join(rtl_dir, top_rtl_file)

        log_path = os.path.join(rtl_dir, "rtl_agent_compile.log")
        compile_status = "Compile not run yet."

        if not os.path.exists(top_rtl_path):
            issues.append(f"❌ Top RTL file missing after generation: {top_rtl_file}")
        if not artifact_list:
            issues.append("❌ No RTL files materialized to disk.")

        if mode == "hierarchical":
            top_file = _top_rtl_file(spec_json, mode)
            top_code = verilog_map.get(top_file, "")
            owned_top_signals = []
            top_name = _top_module_name(spec_json, mode)

            for o in spec_json.get("signal_ownership", []):
                sig = _normalize_signal_token(o.get("signal", ""))
                owner = o.get("owner", "")
                if owner and "." in owner:
                    omod, _ = owner.split(".", 1)
                    if omod != top_name:
                        owned_top_signals.append(sig)

            if re.search(r"\balways\b", top_code):
                for sig in set(owned_top_signals):
                    if re.search(rf"\b{re.escape(sig)}\s*<=", top_code) or re.search(rf"\b{re.escape(sig)}\s*=", top_code):
                        issues.append(f"❌ Top module appears to procedurally drive child-owned signal '{sig}'.")

        compile_cmd = ["iverilog", "-g2005", "-o", os.path.join(rtl_dir, "rtl_out")] + artifact_list
        try:
            cp = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
            compile_status = (cp.stdout or "") + "\n" + (cp.stderr or "")
            if cp.returncode != 0:
                issues.append("❌ Icarus Verilog compile failed.")
        except Exception as e:
            compile_status = f"Compile invocation failed: {e}"
            issues.append(f"❌ Compile invocation failed: {e}")

        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(compile_status.strip() + "\n")
            if issues:
                logf.write("\nIssues:\n")
                for issue in issues:
                    logf.write(f"{issue}\n")

        summary_file = os.path.join(rtl_dir, "rtl_agent_summary.txt")
        with open(summary_file, "w", encoding="utf-8") as sf:
            sf.write("RTL Agent Summary\n")
            sf.write("=================\n")
            sf.write(f"Mode: {mode}\n")
            sf.write(f"Top module: {_top_module_name(spec_json, mode)}\n")
            sf.write(f"Expected files: {expected_files}\n")
            sf.write(f"Materialized files: {[os.path.basename(p) for p in artifact_list]}\n")
            sf.write(f"Clock ports: {sorted(set(clock_ports))}\n")
            sf.write(f"Reset ports: {sorted(set(reset_ports))}\n")
            sf.write(f"Issue count: {len(issues)}\n")
            if issues:
                sf.write("\nIssues:\n")
                for issue in issues:
                    sf.write(f"- {issue}\n")

        for path in artifact_list:
            try:
                with open(path, "r", encoding="utf-8") as vf:
                    save_text_artifact_and_record(
                        workflow_id=workflow_id,
                        agent_name=agent_name,
                        subdir="rtl",
                        filename=os.path.basename(path),
                        content=vf.read(),
                    )
            except Exception as e:
                print(f"⚠️ Failed to upload RTL artifact {path}: {e}")

        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)

        state.update({
            "rtl_output_dir": rtl_dir,
            "rtl_files": artifact_list,
            "artifact": rtl_dir,
            "artifact_list": artifact_list,
            "artifact_log": log_path,
            "port_list": sorted(set(clock_ports + reset_ports)),
            "clock_ports": sorted(set(clock_ports)),
            "reset_ports": sorted(set(reset_ports)),
            "issues": issues,
            "status": "✅ RTL generation complete" if not issues else "⚠ RTL generation completed with issues",
            "digital_rtl_generated": True,
            "digital_rtl_dir": rtl_dir,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        return state

    except Exception as e:
        return _fail_and_upload("Unhandled RTL agent exception after LLM generation.", e)