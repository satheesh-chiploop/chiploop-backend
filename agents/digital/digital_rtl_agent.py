import os
import re
import json
import datetime
import subprocess
import logging
import shutil
logger = logging.getLogger("chiploop")
from typing import Dict, List, Tuple, Optional

from portkey_ai import Portkey

from agents.runtime import RUNTIME_ACTIVE_STATE_KEY, AgentContext, execute_agent
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital RTL Agent"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")

def _stage(msg: str):
    """
    Lightweight stage logger (same pattern as spec agent)
    """
    try:
        logger.info(f"[RTL DEBUG] {msg}")
    except Exception:
        print(f"[RTL DEBUG] {msg}")

def _strip_verilog_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


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

FATAL RTL CORRECTNESS RULES (HIGHEST PRIORITY)

The generated RTL must pass both:
1. Icarus Verilog compile
2. fatal Verilator lint

These rules override stylistic preferences.

A. SINGLE LEGAL OWNER PER SIGNAL (MANDATORY)
Every signal must have exactly one legal owner and exactly one legal driving style.

Allowed ownership styles:
- sequential register/state/output:
  assigned only with nonblocking <= in exactly one clocked always @(posedge clk or negedge rst_n) block
- combinational signal/output:
  assigned only with blocking = in exactly one always @(*) block with full default assignments
- structural wire:
  driven only by assign statements or module port connectivity, and never by procedural blocks

Forbidden:
- assigning the same signal with both = and <=
- assigning the same signal in both a clocked block and a combinational block
- assigning the same signal in two different always blocks
- procedurally driving a signal that is already driven structurally
- driving a child-owned signal again in the parent/top

If a signal is a child output or an inter-module wire, keep it as structural wiring only.
If a signal is declared as a stored register/state element, drive it from one clocked block only.
If a signal is a combinational decode/output, drive it from one always @(*) block only.

B. TOP/HIERARCHY OWNERSHIP DISCIPLINE (MANDATORY)
In hierarchical designs:
- the top module must not procedurally assign, reset, or re-drive any signal owned by a child module
- if signal_ownership says a child owns a signal, the top may only expose it through structural wiring
- do not convert child outputs into top-level procedural regs unless the spec explicitly requires that

C. FSM / OUTPUT STYLE DISCIPLINE
For FSM-controlled or decoded outputs, choose exactly one style per signal:
- registered output in one clocked block only
OR
- combinational output in one always @(*) block only

Do not mix reset-time <= assignments with combinational = assignments for the same output.

D. MULTI-DRIVER BAN
Every signal must have exactly one legal driver.
No signal may be driven by:
- two always blocks
- assign plus always block
- child output plus top assign
- child output plus top procedural block
- two child outputs
- clocked block plus combinational block

E. COMBINATIONAL SAFETY
Every combinational always @(*) block must:
- assign defaults at block entry
- assign every driven signal on all paths
- include a default branch in every case

CONCRETE GOOD/BAD EXAMPLES

BAD:
reg irq;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= done;
end
always @(*) begin
  irq = fault;
end

GOOD:
reg irq;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= (done | fault);
end

BAD:
wire child_irq;
assign irq = child_irq;
always @(*) begin
  irq = 1'b0;
end

GOOD:
wire child_irq;
assign irq = child_irq;

BAD:
reg status_reg;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else if (adc_done) status_reg <= 8'h01;
end
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else if (ana_fault) status_reg <= 8'h02;
end

GOOD:
reg [7:0] status_reg;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= 8'h00;
  else begin
    status_reg[0] <= adc_done;
    status_reg[1] <= ana_fault;
  end
end

B. OUTPUT / WIRE / REG ROLE DISCIPLINE
- If a signal is a pure structural connection between modules, keep it as a wire and do not assign it in always blocks.
- If a module output is driven from sequential logic, declare and drive it as a reg-style procedural output and do not also assign it combinationally.
- Do not declare a top-level wiring signal and then also reset or assign it procedurally.

C. FSM OUTPUT DISCIPLINE
For FSM-controlled outputs:
- either register them in the clocked block using <= only
- or compute them combinationally in always @(*) with blocking = only and full default assignments
- do not mix the two styles for the same output

D. MULTI-DRIVER BAN
Every signal must have exactly one legal driver.
No signal may be driven by:
- two always blocks
- child output plus top assign
- child output plus top procedural block
- assign plus procedural block

E. CASE / COMBINATIONAL SAFETY
Every combinational always @(*) block must:
- assign defaults at block entry
- assign every driven signal on all paths
- include a default branch in every case

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
- Every declared output must have exactly one legal driver.
- In structural top modules, outputs may be exposed through wiring from the owning child module.
- Do not force procedural driving at the top unless the top module owns the signal.
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
UNUSED SIGNAL HYGIENE
- Every declared input, status input, and internal register should be either:
  - functionally used in logic, or
  - intentionally consumed in a harmless deterministic way so that lint does not report it as unused.
- For smoke-test stubs, avoid leaving declared ports completely unused when a simple legal reference can be added.
- Example acceptable pattern:
  - use a signal in a benign conditional branch
  - fold status inputs into a readback/status register if consistent with the spec
- Do not add fake functionality just to silence lint, but avoid trivially unused declared signals when possible.
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
- Output must be fully compile-ready Verilog-2005 with no placeholders.
- NEVER emit pseudo-code, TODOs, comments, or template text in place of expressions.
- Forbidden examples:
  - /* condition */
  - /* address */
  - /* data */
  - TODO
  - implement here
  - fill in later
- Every assignment RHS must be a legal Verilog expression.
- If protocol behavior is underspecified, generate the simplest deterministic legal stub logic instead of placeholders.
- For smoke-test RTL, it is acceptable to drive outputs to constant-safe defaults or simple 
- Do NOT derive semantic configuration or control signals from raw bus signals unless explicitly defined in DIGITAL_SPEC_JSON.
- If a module input is named cfg_*, enable, start, mode, threshold, data, etc.,
  it MUST be driven from an explicitly declared inter_module signal source.
- Forbidden example:
  cfg_* ← reg_wdata[x:y]
- If contract is missing, do NOT guess mapping.
  Use constant-safe default instead of inventing semantic meaning.
  - Distinguish raw external signals from derived internal signals.
- If an inter-module signal is owned by a child module according to signal_ownership, the top module MUST NOT recreate, shortcut, alias, or directly assign that signal from a top-level input or any other source.
- The top module may only connect child-owned internal signals structurally through wires and port connections.

DECLARED PORT COMPLETENESS RULES (MANDATORY)

- Every declared output port must be explicitly driven in the final RTL.
- Every declared input port must be:
  - used in functional logic, or
  - reflected in a specified status/readback path, or
  - intentionally tied into a benign deterministic condition that is consistent with the spec.
- Do not leave any declared output undriven.
- Do not leave any declared input completely unused if the spec gives it behavioral meaning.

For flat single-module register-based peripherals:
- if a control/output signal is listed in the interface, define its exact register or logic source
- if a status/data input is listed in the interface, define where it is captured or exposed in readback
- if a readiness/fault/done input is listed, define whether it affects control gating, status bits, or interrupt generation

DECLARED PORT USAGE EXAMPLES

BAD:
input        ana_ready;
output reg   dac_enable;
// ana_ready never used
// dac_enable never assigned

GOOD:
input        ana_ready;
output reg   dac_enable;

always @(*) begin
  dac_enable = control_reg[2] & ana_ready;
end

BAD:
input  [11:0] adc_data;
input         adc_done;
reg    [11:0] adc_data_reg;

// adc_data declared but never captured

GOOD:
input  [11:0] adc_data;
input         adc_done;
reg    [11:0] adc_data_reg;

always @(posedge clk or negedge rst_n) begin
  if (!rst_n)
    adc_data_reg <= 12'h000;
  else if (adc_done)
    adc_data_reg <= adc_data;
end

BAD:
always @(*) begin
  case (paddr)
    8'h00: prdata = control_reg;
    8'h04: prdata = status_reg;
  endcase
end

GOOD:
always @(*) begin
  prdata = 32'h00000000;
  case (paddr)
    8'h00: prdata = control_reg;
    8'h04: prdata = status_reg;
    default: prdata = 32'h00000000;
  endcase
end

PROCEDURAL OUTPUT DECLARATION RULES (MANDATORY)

- If an output is assigned inside any always @(*) block or any clocked always block, that output must be declared as a reg-style procedural output in Verilog-2005.
- Do NOT declare an output as a plain wire-style output if it is assigned procedurally.
- If an output is driven only by a continuous assign statement, it may remain a plain output wire-style port.
- Never procedurally assign to a wire-style output.


GOOD:
output reg [31:0] prdata;
always @(*) begin
  prdata = 32'h00000000;
  case (paddr)
    ...
    default: prdata = 32'h00000000;
  endcase
end

GOOD:
output [31:0] prdata;
assign prdata = prdata_mux;

BAD:
output [31:0] prdata;
always @(*) begin
  prdata = 32'h00000000;
end

BAD:
output [31:0] prdata;
wire [31:0] prdata_temp;
always @(*) begin
  prdata_temp = 32'h0;
end
assign prdata = prdata_temp;

GOOD:
output [31:0] prdata;
reg [31:0] prdata_temp;
always @(*) begin
  prdata_temp = 32'h0;
end
assign prdata = prdata_temp;

INTERNAL SIGNAL ROLE SEPARATION RULES (MANDATORY)

- Distinguish:
  1. externally visible top-level ports
  2. internal inter-module signals
  3. decoded control/configuration signals
  4. status/derived/behavioral outputs

- Do NOT reuse one signal for multiple unrelated roles unless explicitly defined in DIGITAL_SPEC_JSON.
- A decoded control/config signal must remain a dedicated internal signal unless explicitly defined as a top-level port.
- A behavioral/status/output signal must not be reused as an unrelated internal control/config signal.
- If one module produces a signal and another consumes it, connect them through a dedicated internal wire.
- Never merge signals just because names or widths look similar.
- Never alias two signals unless the contract explicitly defines them as the same.

DECODED REGISTER SIGNAL WIDTH RULES (MANDATORY)

- If multiple semantic signals are decoded from a register, the register must be wide enough.
- Do not declare a scalar if indexed bits are used.
- Bit/part selections must match declared widths.
- Concatenations must match destination width exactly.
- Do not rely on implicit truncation/expansion.

- Example:
  If adc_done_sync is owned by analog_if_logic.adc_done_sync, then the top module must connect:
    analog_if_logic.adc_done_sync -> internal wire adc_done_sync
  and then consume that wire in downstream modules.
  The top module MUST NOT do:
    assign adc_done_sync = adc_done;
    assign adc_done_sync = ana_done;
    assign adc_done_sync = any_top_input;

- The same rule applies to synchronized, decoded, filtered, qualified, or derived signals such as:
  *_sync
  *_status
  *_irq
  *_valid
  *_ready
  *_fault
  *_done

- If a signal name appears in inter_module_signals and signal_ownership, then:
  1. create exactly one internal wire of that name
  2. connect the declared owner port to that wire
  3. connect that wire to the declared consumer ports
  4. do not add any extra assign or procedural driver for that signal
DERIVED SIGNAL OWNERSHIP RULE:
If a submodule produces synchronized, decoded, filtered, qualified, or derived versions of top-level inputs (for example *_sync, *_status, *_irq, *_valid, *_ready, *_fault, *_done), then those derived outputs must appear explicitly in:
- module ports
- inter_module_signals
- signal_ownership
and must be owned by the producing submodule, not by the top module.
- If multiple semantic outputs are decoded from one writable register (for example cfg_enable, cfg_adc_start, cfg_dac_enable), the backing register MUST be declared as a vector wide enough to hold all referenced bits.
- Never declare a backing register as a scalar if any code reads indexed bits from it.
- Examples:
  - If RTL uses control_reg[0], control_reg[1], or control_reg[2], then control_reg must be declared at least as reg [2:0] control_reg;
  - If the register is software-visible as an 8-bit register, prefer reg [7:0] control_reg;

- Any signal used with bit selection [N] or part selection [M:N] MUST be declared as a vector of sufficient width.
- Never emit code that indexes into a scalar reg or wire.

- For register-backed semantic config outputs:
  - cfg_enable may be driven from control_reg[0]
  - cfg_adc_start may be driven from control_reg[1]
  - cfg_dac_enable may be driven from control_reg[2]
  only if control_reg is declared with sufficient width.

- When reconstructing a wider configuration value from byte registers, the concatenation width MUST exactly equal the destination width.
- Example:
  - if cfg_dac_code is [11:0]
  - and dac_code_l is [7:0]
  - and dac_code_h is [7:0] but only lower nibble is valid
  - then cfg_dac_code must be {{dac_code_h[3:0], dac_code_l[7:0]}}
- Never assign {{dac_code_h, dac_code_l}} to a 12-bit destination.
 Example:
  If software writes an 8-bit CONTROL register and RTL decodes bits [0], [1], and [2], declare:
    reg [7:0] control_reg;
  not:
    reg control_reg;

- Protocol-facing modules (such as i2c_slave, spi_slave, uart_rx, uart_tx, bus_adapter, decoder, bridge, handshake controllers, or similar) must still be fully compile-ready Verilog-2005.
- If the protocol behavior is not fully specified, DO NOT emit protocol pseudo-code, comment placeholders, or template conditions.
- In particular, NEVER emit executable constructs such as:
  if (/* ... */)
  while (/* ... */)
  case (/* ... */)
  assign x = /* ... */;
- For underspecified protocol modules, generate the simplest deterministic legal smoke-test stub that compiles cleanly.
- Acceptable fallback behavior for an underspecified protocol slave:
  - hold outputs at reset-safe defaults
  - deassert reg_wr_en and reg_rd_en by default
  - hold reg_addr and reg_wdata at zero unless a fully defined legal condition is available
  - keep state transitions driven only by legal constants or declared signals
- If you cannot justify a real transaction condition from the spec, use a compile-safe constant-false condition such as:
  if (1'b0) begin
  rather than any placeholder text.
- Every if/else/case condition must be a legal Verilog expression using only literals, declared signals, parameters, or valid operators.

1. FSM coding rules
- For every FSM, use a standard 2-process style:
  a) one sequential always block for state registers:
     always @(posedge clk or negedge rst_n)
  b) one combinational always @(*) block for next_state and combinational outputs
- In every combinational always @(*) block, assign safe default values at the top BEFORE the case statement:
  - next_state must get a default assignment
  - every combinational output driven in that block must get a default assignment
- Every signal assigned in a combinational block must be assigned on all paths.
- Do NOT generate combinational blocks that leave outputs unassigned in any case/default branch.
- Do NOT generate latch-prone RTL.

2. Case statement rules
- Every case statement must include a default branch.
- Do not leave read/write decode cases incomplete.
- For register read muxes, provide a deterministic default read value.
- For write decodes, include a default no-op branch.

3. Width and assignment rules
- All assignments must be width-correct.
- Do not assign narrower concatenations into wider registers without explicit zero-extension.
- Match declared signal widths exactly.
- Avoid implicit truncation/expansion unless explicitly intended and coded.

4. Register map rules
- Separate control and status behavior clearly.
- If status registers pack status bits into a wider register, explicitly zero-pad unused upper bits.
- Register read data must be assigned deterministically.
- Avoid incomplete register decode logic.

6. Output requirement
Before finalizing the RTL, self-check:
- no latch inference
- no incomplete combinational assignments
- default branch exists in every case
- widths are consistent
- top/module/port names exactly match the spec JSON

FSM SAFETY RULES

- In every combinational always @(*) block:
  - assign default values at block entry
  - assign all outputs on all paths
- No latch inference is allowed.

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
14. No comments or placeholder text may appear inside executable expressions.
15. No assignment may contain /* ... */ on the RHS.
16. If protocol details are unknown, emit a minimal deterministic compile-safe implementation, not pseudo-code.
17. For every inter-module signal owned by a child module:
    - the top module contains exactly one wire of that signal name
    - the wire is driven only by the declared owner child port
    - the wire is consumed only by the declared destination ports
    - there is no assign statement or always block in the top module that re-drives or aliases that signal

18. Never connect a *_sync signal directly from a raw top-level input unless the spec explicitly declares that raw input as the owner.
19. Every signal used with bit indexing or slicing must be declared as a vector of sufficient width; never index into a scalar.
20. Any backing register used to decode multiple semantic outputs must be declared wide enough for all referenced bit positions.
21. For every assignment, LHS width and RHS width must match exactly after slicing/concatenation.
22. If cfg_* outputs are derived from a writable control register, the control register declaration and bit usage must be mutually consistent.
23. No executable statement may contain comment text as an expression or condition.
24. Every if, case, while, and ternary condition must be a legal Verilog expression.
25. For underspecified protocol modules, emit a compile-safe stub, not protocol pseudo-code.
26. Search generated RTL for forbidden placeholder patterns before output:
    - if (/*
    - case (/*
    - = /* 
    - TODO
    - implement here
    - some condition
27. No signal is reused for multiple unrelated roles unless explicitly defined.
28. No top-level port is reused as an unrelated internal signal.
29. No decoded control signal is aliased onto an unrelated output.
30. Every signal has exactly one legal driver.
31. Structural top modules do not convert child outputs into procedural top-level regs unless required.
PROCEDURAL OUTPUT CONSISTENCY SELF-CHECK (MANDATORY)

Before returning RTL, verify this exact rule for every output:
- if the output appears on the left-hand side inside any always block, it must be declared as output reg in Verilog-2005
- if the output is declared as plain output, it must not appear on the left-hand side inside any always block
- never return RTL that would cause:
  - "is not a valid l-value"
  - PROCASSWIRE

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
        "rtl_verilator_lint.log",
        "rtl_agent_summary.txt",
        "rtl_agent_exception.txt",
        "rtl_llm_raw_output.txt",
        "rtl_agent_compile_pass2.log",
        "rtl_verilator_lint_pass2.log",
        "rtl_agent_summary_pass2.txt",
        "rtl_agent_exception_pass2.txt",
        "rtl_llm_raw_output_pass2.txt",
        "rtl_agent_final_status.log",
        "rtl_agent_final_summary.txt",
    ]:
        _record_text_artifact_safe(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="rtl",
            filename=fname,
            path=os.path.join(rtl_dir, fname),
        )

def _append_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


def _build_rtl_repair_prompt(base_prompt: str, previous_llm_output: str, compile_log_text: str, verilator_log_text: str) -> str:
    return f"""
{base_prompt}

==============================
REPAIR MODE (SECOND PASS)
==============================

Your previous RTL output failed one or more correctness gates.

You MUST preserve the same architecture unless a structural change is strictly required to fix the errors.

PREVIOUS RTL OUTPUT:
{previous_llm_output}

ICARUS COMPILE LOG:
{compile_log_text}

FATAL VERILATOR LOG (if any):
{verilator_log_text}

REPAIR RULES:
- Do NOT redesign the architecture unless required to resolve correctness errors
- Preserve module names, filenames, ports, hierarchy, and wiring intent as much as possible
- Fix only:
  - Icarus compile failures
  - fatal Verilator errors
  - structural/spec mismatch issues
- Do NOT spend tokens cleaning non-fatal lint warnings only
- Return ONE full corrected response in the same named-file-block format
- Do NOT return explanations
- Do NOT return partial edits

PRIMARY OBJECTIVE:
Make the MINIMUM NECESSARY change to fix correctness errors.

- Do NOT redesign architecture
- Do NOT rename modules/ports/files
- Do NOT rewrite unaffected files
- Prefer local fixes over global rewrites

CORRECTNESS REPAIR PRIORITIES (MANDATORY)

TARGETED REPAIR PROCEDURE FOR FATAL LINT / COMPILE ERRORS

1. Read the exact failing signal names from the compile and Verilator logs.
2. For each failing signal, classify it into exactly one category:
   - structural wire / child-owned connection
   - combinational signal
   - sequential registered signal
3. Rewrite that signal so it uses exactly one legal driving style:
   - structural wire -> assign / module port wiring only
   - combinational signal -> blocking = only in exactly one always @(*)
   - sequential signal -> nonblocking <= only in exactly one clocked always block
4. Remove all conflicting assignments to that signal everywhere else.
5. If a signal is owned by a child module or by explicit signal_ownership, do not also reset, assign, or procedurally drive it in the parent/top.
6. If Verilator reports BLKANDNBLK, the repair is incomplete unless every reported signal has only one assignment style in the final RTL.
7. If Verilator reports MULTIDRIVEN or multiple procedural drivers, the repair is incomplete unless all duplicate drivers are removed from the final RTL.

MANDATORY REPAIR OVERRIDE
If the previous RTL uses an illegal ownership pattern, you MUST rewrite the affected block structure enough to eliminate the illegal drivers.
Preserving the previous block structure is NOT allowed if it leaves:
- one signal assigned in multiple always blocks
- one signal assigned in both clocked and combinational logic
- one signal driven both structurally and procedurally
- a child-owned signal driven again in the parent/top

SPECIAL REPAIR RULE FOR HIERARCHICAL TOPS
In hierarchical mode, if a top-level signal is sourced from a child output or child-owned internal signal:
- keep that signal as structural wiring only
- remove any top-level reset assignment, combinational assignment, or extra assign to that same signal

SPECIAL REPAIR RULE FOR REGISTER/STATUS LOGIC
If multiple clocked blocks update the same stored register or status/output register:
- merge those updates into one legal clocked always block
- do not keep separate clocked writers for the same signal

WARNING CLEANUP RULE FOR DECLARED PORTS
If Verilator reports:
- UNDRIVEN on a declared output, the repair is incomplete until that output has an explicit legal driver.
- UNUSEDSIGNAL on a declared input with behavioral meaning from the spec, the repair is incomplete until that input is functionally consumed or exposed through status/readback.
- CASEINCOMPLETE, the repair is incomplete until a default branch is present.

GOOD/BAD REPAIR EXAMPLES

BAD REPAIR:
always @(posedge clk or negedge rst_n) irq <= done;
always @(*) irq = fault;

GOOD REPAIR:
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) irq <= 1'b0;
  else irq <= (done | fault);
end

BAD REPAIR:
assign irq = child_irq;
always @(*) irq = masked_irq;

GOOD REPAIR:
assign irq = child_irq;

BAD REPAIR:
always @(posedge clk or negedge rst_n) status_reg <= next_status_a;
always @(posedge clk or negedge rst_n) status_reg <= next_status_b;

GOOD REPAIR:
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) status_reg <= RESET_VALUE;
  else status_reg <= merged_next_status;
end

PROCEDURAL OUTPUT REPAIR RULE
- If compile or lint shows PROCASSWIRE, rewrite the affected signal so that:
  - either it becomes a reg-style procedural output and remains procedurally assigned
  - or it is moved to a continuous assign path and is no longer assigned in always blocks
- The repair is incomplete if a procedurally assigned signal remains declared as a wire-style output.

REPAIR PRIORITY ORDER
1. BLKANDNBLK
2. MULTIDRIVEN / illegal multiple drivers
3. undeclared or illegal reg/wire usage
4. incomplete combinational assignments / latch-prone logic
5. case completeness / default branches
6. width mismatches

WARNING-ONLY LINT REPAIR RULE
- If compile passes and the Verilator log contains only warning classes such as UNUSEDSIGNAL, do not redesign the RTL.
- Prefer minimal local fixes only if they are straightforward and preserve behavior.
- Do not perform broad rewrites for warning-only lint.


When repairing RTL, fix these classes of issues first:


1. FSM / latch issues
- Eliminate latch-prone combinational FSM logic.
- In every combinational FSM block:
  - assign next_state a default value first
  - assign every combinational output a default value first
  - keep assignments complete across all branches
- Preserve the same FSM architecture and state names unless a change is strictly required.

2. Register map correctness
- Fix width mismatches by using explicit zero-padding or width-correct assignments.
- Add missing default branches in register decode case statements.
- Ensure reg_rdata and similar outputs are assigned deterministically.

3. Structural correctness only
- Preserve filenames, module names, ports, and hierarchy.
- Do NOT redesign the architecture to fix non-fatal lint style issues.
- Fix only correctness blockers:
  - compile failures
  - fatal lint/elaboration issues
  - spec/structure mismatches
  - latch-prone coding
  - width/signing mistakes that can break synthesis or behavior

  SIGNAL ROLE / MULTI-DRIVER REPAIR RULES

- If one signal is used for multiple unrelated roles, split it into separate signals.
- Introduce internal wires instead of reusing top-level ports.
- If a signal is produced by one module and consumed by another, connect via a dedicated internal wire.
- Never allow multiple drivers on the same signal.
- Do NOT fix wiring bugs by converting outputs to reg at top-level.

Fix only:
- Icarus compile failures
- fatal Verilator errors
- structural/spec mismatches
- latch-prone coding
- illegal multi-driver issues
- illegal reg/wire usage

Do NOT spend effort fixing non-fatal lint warnings.

SELF-CHECK BEFORE RETURNING RTL
- No latch inference from incomplete combinational assignments
- Every combinational case has a default branch
- Every combinationally driven output has a default assignment at block entry
- Packed status/control register assignments are width-correct
- Minimal changes applied
- No architecture changes
- No multi-driver signals
- No illegal reg/wire connections
- No latch inference
- Interfaces unchanged
""".strip()


def _run_verilator_lint(rtl_dir: str, verilog_files: List[str], top_module: str, suffix: str = "") -> Tuple[bool, str, str]:
    log_name = "rtl_verilator_lint.log" if not suffix else f"rtl_verilator_lint_{suffix}.log"
    lint_log_path = os.path.join(rtl_dir, log_name)

    if not verilog_files:
        msg = "No Verilog files provided to Verilator lint.\n"
        with open(lint_log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        return False, lint_log_path, msg

    # When cwd=rtl_dir, pass only basenames (or absolute paths), not rtl_dir-prefixed relative paths.
    verilator_inputs = []
    for vf in verilog_files:
        if os.path.isabs(vf):
            verilator_inputs.append(vf)
        else:
            verilator_inputs.append(os.path.basename(vf))

    cmd = [
        "verilator",
        "--lint-only",
        "-Wall",
        "-Wno-fatal",
        "--top-module",
        top_module,
    ] + verilator_inputs

    logger.info(
        f"[RTL DEBUG] running_verilator_lint suffix={suffix or 'pass1'} "
        f"top={top_module} file_count={len(verilog_files)}"
    )
    logger.info(f"[RTL DEBUG] verilator_cmd={' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=rtl_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        msg = "Verilator executable not found in PATH.\n"
        with open(lint_log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        logger.error("[RTL DEBUG] verilator_not_found")
        return False, lint_log_path, msg
    except Exception as e:
        msg = f"Verilator execution failed: {e}\n"
        with open(lint_log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        logger.error(f"[RTL DEBUG] verilator_exception: {e}")
        return False, lint_log_path, msg

    combined = ""
    if proc.stdout:
        combined += "=== STDOUT ===\n" + proc.stdout + "\n"
    if proc.stderr:
        combined += "=== STDERR ===\n" + proc.stderr + "\n"
    combined += f"=== RETURN CODE ===\n{proc.returncode}\n"

    with open(lint_log_path, "w", encoding="utf-8") as f:
        f.write(combined)

    if proc.returncode != 0:
        logger.error(f"[RTL DEBUG] verilator_failed suffix={suffix or 'pass1'} rc={proc.returncode}")
        return False, lint_log_path, combined

    logger.info(f"[RTL DEBUG] verilator_passed suffix={suffix or 'pass1'}")
    return True, lint_log_path, combined



def _classify_verilator_result(verilator_ok: bool, verilator_output: str) -> str:
    """
    Returns one of:
      - "pass"
      - "warning_only"
      - "fatal"
    """
    if verilator_ok:
        return "pass"

    text = verilator_output or ""

    if "%Error:" in text:
        # Treat warnings-only termination as warning_only
        if "Exiting due to" in text and "warning(s)" in text and "%Error:" == "%Error:":
            non_warning_errors = [
                line for line in text.splitlines()
                if "%Error:" in line and "Exiting due to" not in line
            ]
            if not non_warning_errors:
                return "warning_only"
        return "fatal"

    fatal_patterns = [
        "Cannot find file containing module",
        "syntax error",
        "Internal Error",
    ]

    for pat in fatal_patterns:
        if pat in text:
            return "fatal"

    return "warning_only"


def _promote_rtl_files_to_root(rtl_dir: str, artifact_list: List[str]) -> List[str]:
    promoted = []
    os.makedirs(rtl_dir, exist_ok=True)

    for src in artifact_list:
        dst = os.path.join(rtl_dir, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        promoted.append(dst)

    return promoted

def _validate_and_materialize_rtl(
    llm_output: str,
    rtl_dir: str,
    spec_json: dict,
    mode: str,
    suffix: str = "",
    materialize_subdir: str = "",
) -> dict:
    raw_name = "rtl_llm_raw_output.txt" if not suffix else f"rtl_llm_raw_output_{suffix}.txt"
    compile_log_name = "rtl_agent_compile.log" if not suffix else f"rtl_agent_compile_{suffix}.log"
    summary_name = "rtl_agent_summary.txt" if not suffix else f"rtl_agent_summary_{suffix}.txt"

    raw_output_path = os.path.join(rtl_dir, raw_name)
    with open(raw_output_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    verilog_map = _parse_named_verilog_blocks(llm_output)
    if not verilog_map:
        return {
            "ok": False,
            "message": "LLM output did not contain any named Verilog file blocks in the required format.",
            "issues": ["❌ Missing named Verilog file blocks in LLM output."],
            "compile_log_path": os.path.join(rtl_dir, compile_log_name),
            "summary_path": os.path.join(rtl_dir, summary_name),
            "raw_output_path": raw_output_path,
            "artifact_list": [],
        }

    expected_files = _collect_expected_rtl_files(spec_json, mode)
    artifact_list = []

    materialize_dir = rtl_dir if not materialize_subdir else os.path.join(rtl_dir, materialize_subdir)
    os.makedirs(materialize_dir, exist_ok=True)
    for fname in expected_files:
        code = verilog_map.get(fname)
        if not code:
            continue
        fpath = os.path.join(materialize_dir, fname)
        with open(fpath, "w", encoding="utf-8") as vf:
            vf.write(code + "\n")
        artifact_list.append(fpath)

    top_rtl_file = _top_rtl_file(spec_json, mode)
    top_rtl_path = os.path.join(materialize_dir, top_rtl_file)

 

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
    scan_text = _strip_verilog_comments(full_text)

    for pat in forbidden_sv_patterns:
        if pat == r"\blogic\b":
            if re.search(r"\blogic\s+(\[[^\]]+\]\s+)?[A-Za-z_]\w*", scan_text):
                issues.append(f"❌ Forbidden SystemVerilog construct found in RTL: pattern '{pat}'")
        else:
            if re.search(pat, scan_text):
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
    top_rtl_path = os.path.join(materialize_dir, top_rtl_file)

    compile_log_path = os.path.join(rtl_dir, compile_log_name)
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

        _stage(f"iverilog_compile_start_{suffix or 'pass1'}")
    compile_cmd = [
        "iverilog",
        "-g2005",
        "-o",
        os.path.join(rtl_dir, f"rtl_out{('_' + suffix) if suffix else ''}")
    ] + artifact_list

    iverilog_failed = False
    try:
        cp = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        compile_status = (cp.stdout or "") + "\n" + (cp.stderr or "")
        if cp.returncode != 0:
            iverilog_failed = True
            issues.append("❌ Icarus Verilog compile failed.")
            _stage(f"iverilog_compile_failed_{suffix or 'pass1'}")
        else:
            _stage(f"iverilog_compile_passed_{suffix or 'pass1'}")
    except Exception as e:
        iverilog_failed = True
        compile_status = f"Compile invocation failed: {e}"
        issues.append(f"❌ Compile invocation failed: {e}")
        _stage(f"iverilog_compile_exception_{suffix or 'pass1'}")

    with open(compile_log_path, "w", encoding="utf-8") as logf:
        logf.write(compile_status.strip() + "\n")
        if issues:
            logf.write("\nIssues:\n")
            for issue in issues:
                logf.write(f"{issue}\n")

    verilator_ok = False
    verilator_log_path = os.path.join(
        rtl_dir,
        "rtl_verilator_lint.log" if not suffix else f"rtl_verilator_lint_{suffix}.log"
    )
    verilator_output = ""
    verilator_severity = "not_run"

    _stage(f"running_verilator_lint_{suffix or 'pass1'}")
    verilator_ok, verilator_log_path, verilator_output = _run_verilator_lint(
        rtl_dir=rtl_dir,
        verilog_files=artifact_list,
        top_module=_top_module_name(spec_json, mode),
        suffix=suffix,
    )

    verilator_severity = _classify_verilator_result(verilator_ok, verilator_output)

    if verilator_severity == "fatal":
        issues.append("❌ Verilator lint failed.")
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT FAILURE ===\n"
            "Fatal Verilator issues detected. See corresponding rtl_verilator_lint log for details.\n"
        )
        _stage(f"verilator_lint_fatal_{suffix or 'pass1'}")

    elif verilator_severity == "warning_only":
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT WARNINGS ===\n"
            "Non-fatal Verilator warnings detected.\n"
        )
        _stage(f"verilator_lint_warning_only_{suffix or 'pass1'}")

    else:
        _append_text(
            compile_log_path,
            "\n=== VERILATOR LINT ===\n"
            "PASS: Verilator lint completed successfully.\n"
        )
        _stage(f"verilator_lint_passed_{suffix or 'pass1'}")



    summary_path = os.path.join(rtl_dir, summary_name)
    with open(summary_path, "w", encoding="utf-8") as sf:
        sf.write("RTL Agent Summary\n")
        sf.write("=================\n")
        sf.write(f"Mode: {mode}\n")
        sf.write(f"Top module: {_top_module_name(spec_json, mode)}\n")
        sf.write(f"Expected files: {expected_files}\n")
        sf.write(f"Materialized files: {[os.path.basename(p) for p in artifact_list]}\n")
        sf.write(f"Clock ports: {sorted(set(clock_ports))}\n")
        sf.write(f"Reset ports: {sorted(set(reset_ports))}\n")
        sf.write(f"Icarus compile: {'fail' if iverilog_failed else 'pass'}\n")
        sf.write(f"Verilator lint: {verilator_severity}\n")
        sf.write(f"Issue count: {len(issues)}\n")
        if issues:
            sf.write("\nIssues:\n")
            for issue in issues:
                sf.write(f"- {issue}\n")


    return {
        "ok": len(issues) == 0,
        "message": "RTL checks passed." if len(issues) == 0 else "RTL checks failed.",
        "issues": issues,
        "artifact_list": artifact_list,
        "clock_ports": sorted(set(clock_ports)),
        "reset_ports": sorted(set(reset_ports)),
        "compile_log_path": compile_log_path,
        "summary_path": summary_path,
        "raw_output_path": raw_output_path,
        "verilator_log_path": verilator_log_path,
        "verilator_output": verilator_output,
        "verilator_severity": verilator_severity,
        "llm_output": llm_output,
        "verilog_map": verilog_map,
    }


def _run(context: AgentContext) -> dict:
    state = context.state
    agent_name = context.agent_name
    print("\n🧠 Running RTL Agent (implementation mode).")



    _stage("entered_run_agent")

    workflow_id = context.workflow_id
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Restore local directory structure
    rtl_dir = os.path.join(workflow_dir, "rtl")
    os.makedirs(rtl_dir, exist_ok=True)

    def _fail_and_upload(msg: str, exc: Exception = None) -> dict:
        # Do NOT overwrite pass1/pass2 logs. Preserve them.
        final_log_path = os.path.join(rtl_dir, "rtl_agent_final_status.log")
        final_summary_path = os.path.join(rtl_dir, "rtl_agent_final_summary.txt")
        error_file = os.path.join(rtl_dir, "rtl_agent_exception.txt")

        with open(final_log_path, "w", encoding="utf-8") as lf:
            lf.write(msg + "\n")
            if exc is not None:
                lf.write(f"Exception type: {type(exc).__name__}\n")
                lf.write(f"Exception: {exc}\n")

        with open(final_summary_path, "w", encoding="utf-8") as sf:
            sf.write("❌ RTL generation failed.\n\n")
            sf.write(msg + "\n")
            if exc is not None:
                sf.write(f"Exception type: {type(exc).__name__}\n")
                sf.write(f"Exception: {exc}\n")

        if exc is not None:
            with open(error_file, "w", encoding="utf-8") as ef:
                ef.write(repr(exc) + "\n")

        _upload_rtl_debug_artifacts(workflow_id, agent_name, rtl_dir)
        _record_text_artifact_safe(workflow_id, agent_name, "rtl", "rtl_agent_final_status.log", final_log_path)
        _record_text_artifact_safe(workflow_id, agent_name, "rtl", "rtl_agent_final_summary.txt", final_summary_path)

        state.update({
            "status": f"❌ RTL generation failed: {msg}",
            "artifact": None,
            "artifact_list": [],
            "artifact_log": final_log_path,
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
        _stage(f"llm_output_pass1_chars: {len(llm_output)}")
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
        _stage("pass1_validate_and_materialize")

        pass1 = _validate_and_materialize_rtl(
            llm_output=llm_output,
            rtl_dir=rtl_dir,
            spec_json=spec_json,
            mode=mode,
            suffix="",
            materialize_subdir="",      # keep pass1 exactly as today
        )


        if not pass1["ok"]:
            _stage("pass1_failed_triggering_pass2")
            logger.warning(f"[RTL DEBUG] pass1_failed issues={len(pass1['issues'])}")

            compile_log_text = ""
            if os.path.exists(pass1["compile_log_path"]):
                with open(pass1["compile_log_path"], "r", encoding="utf-8") as f:
                    compile_log_text = f.read()

            verilator_log_text = ""
            if pass1.get("verilator_severity") == "fatal" and os.path.exists(pass1["verilator_log_path"]):
                with open(pass1["verilator_log_path"], "r", encoding="utf-8") as f:
                    verilator_log_text = f.read()

            repair_prompt = _build_rtl_repair_prompt(
                base_prompt=prompt,
                previous_llm_output=llm_output,
                compile_log_text=compile_log_text,
                verilator_log_text=verilator_log_text,
            )


            _stage("starting_llm_call_pass2")

            
            try:
                completion_pass2 = client_portkey.chat.completions.create(
                    model="@chiploop/gpt-4o-mini",
                    messages=[{"role": "user", "content": repair_prompt}],
                    stream=False,
                )
                llm_output_pass2 = completion_pass2.choices[0].message.content or ""
                _stage(f"llm_output_pass2_chars: {len(llm_output_pass2)}")
            except Exception as e2:
                pass2_exc = os.path.join(rtl_dir, "rtl_agent_exception_pass2.txt")
                pass2_log = os.path.join(rtl_dir, "rtl_agent_compile_pass2.log")
                pass2_summary = os.path.join(rtl_dir, "rtl_agent_summary_pass2.txt")

                with open(pass2_exc, "w", encoding="utf-8") as ef:
                    ef.write(f"RTL pass2 generation exception:\n{repr(e2)}\n")

                with open(pass2_log, "w", encoding="utf-8") as lf:
                    lf.write("RTL pass2 failed before RTL materialization.\n")
                    lf.write(f"Exception type: {type(e2).__name__}\n")
                    lf.write(f"Exception: {e2}\n")

                with open(pass2_summary, "w", encoding="utf-8") as sf:
                    sf.write("❌ RTL pass2 generation failed before raw output was written.\n")
                    sf.write(f"Exception type: {type(e2).__name__}\n")
                    sf.write(f"Exception: {e2}\n")

                return _fail_and_upload("Pass1 failed and Pass2 LLM generation failed.", e2)

            _stage("pass2_validate_and_materialize")

            pass2 = _validate_and_materialize_rtl(
                llm_output=llm_output_pass2,
                rtl_dir=rtl_dir,
                spec_json=spec_json,
                mode=mode,
                suffix="pass2",
                materialize_subdir="pass2", # isolate pass2 RTL
            )

            final_result = pass2 if pass2["ok"] else pass1
            final_suffix = "pass2" if pass2["ok"] else "pass1"

            if pass2["ok"]:
                promoted_files = _promote_rtl_files_to_root(rtl_dir, pass2["artifact_list"])
                final_result["artifact_list"] = promoted_files


            if not pass2["ok"]:
                return _fail_and_upload(
                    "RTL failed checks in both pass1 and pass2."
                )

            _stage("pass2_passed")
        else:
            final_result = pass1
            final_suffix = "pass1"

        artifact_list = final_result["artifact_list"]
        log_path = final_result["compile_log_path"]
        clock_ports = final_result["clock_ports"]
        reset_ports = final_result["reset_ports"]
        issues = final_result["issues"]

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
            "status": f"✅ RTL generation complete ({final_suffix})" if not issues else f"⚠ RTL generation completed with issues ({final_suffix})",
            "digital_rtl_generated": True,
            "digital_rtl_dir": rtl_dir,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir,
        })
        return state

    except Exception as e:
        return _fail_and_upload("Unhandled RTL agent exception after LLM generation.", e)


def run_agent(state: dict) -> dict:
    context = AgentContext.from_state(state, AGENT_NAME)
    if state.get(RUNTIME_ACTIVE_STATE_KEY):
        return _run(context)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state

  
