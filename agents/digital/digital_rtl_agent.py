import os
import re
import json
import datetime
import subprocess
from typing import Dict, List, Tuple, Optional

from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


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
    spec_text = json.dumps(spec_json)

    # Existing useful cfg checks
    for req in ["cfg_enable", "cfg_adc_start", "cfg_dac_enable", "cfg_dac_code"]:
        if req in spec_text and req not in full_text:
            issues.append(f"❌ Expected {req} signal usage not found in RTL.")

    # New checks from explicit top-level and inter-module contracts
    if mode == "hierarchical":
        for c in spec_json.get("top_level_connections", []):
            tp = c.get("top_port")
            if tp and tp not in full_text:
                issues.append(f"⚠ Top-level connection signal '{tp}' not clearly visible in RTL text.")

        for s in spec_json.get("inter_module_signals", []):
            sig_name = s.get("name")
            if sig_name and sig_name not in full_text:
                issues.append(f"❌ Inter-module signal '{sig_name}' not found in RTL.")

        for o in spec_json.get("signal_ownership", []):
            sig = o.get("signal")
            owner = o.get("owner")
            if sig and owner and sig not in full_text:
                issues.append(f"⚠ Owned signal '{sig}' from '{owner}' not found in RTL.")

    return issues, sorted(set(clock_ports)), sorted(set(reset_ports))


def _build_generation_prompt(spec_json: dict, mode: str, regmap_obj: Optional[dict], clock_reset_obj: Optional[dict], power_intent_obj: Optional[dict]) -> str:
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
{json.dumps(spec_json, indent=2)}

DIGITAL_REGMAP_JSON:
{json.dumps(regmap_obj, indent=2) if regmap_obj is not None else "null"}

CLOCK_RESET_ARCH_JSON:
{json.dumps(clock_reset_obj, indent=2) if clock_reset_obj is not None else "null"}

POWER_INTENT_JSON:
{json.dumps(power_intent_obj, indent=2) if power_intent_obj is not None else "null"}

IMPLEMENTATION RULES
- Generate synthesizable Verilog-2005.
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
- Use top_level_connections as hard requirements for how top ports connect into submodules.
- Use inter_module_signals as hard requirements for internal wiring between submodules.
- Use signal_ownership as hard requirements for legal drivers of signals.
- If there is a register map, implement real stored writable registers where required.
- Implement STATUS and INT_STATUS from explicit field semantics if regmap provides them.
- If a wider value is split across byte registers, store and reconstruct it faithfully.
- If a module is the sole owner of an output or signal, no other module may drive it.
- Prefer the simplest deterministic smoke-test implementation consistent with the contract.
- If there is a control interface like cfg_* in the spec/regmap contract, it must actually be wired between modules and used to drive outputs.
- In the hierarchical top module:
  1. instantiate all required submodules,
  2. create internal wires matching inter_module_signals,
  3. connect top-level ports according to top_level_connections,
  4. connect internal source/destination pairs according to inter_module_signals,
  5. expose top outputs only from their declared owners.
- Entire design must compile together cleanly.

SELF-CHECK BEFORE OUTPUT
1. Every expected file is emitted exactly once.
2. Every module name matches spec.
3. Every port list matches spec exactly.
4. No missing or extra ports.
5. No undeclared signals.
6. No width mismatches.
7. Internal control/status signal contracts are actually wired.
8. Stored registers are not faked by directly echoing bus write data on reads.
9. top_level_connections are reflected in the top RTL.
10. inter_module_signals are reflected as actual internal wires and connections.
11. signal_ownership is respected, with one legal driver per owned signal.
""".strip()


def run_agent(state: dict) -> dict:
    agent_name = "Digital RTL Agent"
    print("\n🧠 Running RTL Agent (implementation mode)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec_obj = _load_json_if_path(state.get("digital_spec_json")) or _load_json_if_path(state.get("spec_json"))
    if not spec_obj:
        state["status"] = "❌ Missing digital spec JSON for RTL generation."
        return state

    spec_json, mode = _normalize_spec_json(spec_obj)

    regmap_obj = (
        _load_json_if_path(state.get("digital_regmap_json"))
        or _load_json_if_path(state.get("digital_regmap"))
    )

    clock_reset_obj = _load_json_if_path(state.get("clock_reset_arch_path"))

    power_intent_obj = None
    if isinstance(state.get("signoff", {}).get("power_intent"), dict):
        power_intent_obj = state["signoff"]["power_intent"]

    prompt = _build_generation_prompt(spec_json, mode, regmap_obj, clock_reset_obj, power_intent_obj)

    try:
        completion = client_portkey.chat.completions.create(
            model="@chiploop/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        llm_output = completion.choices[0].message.content or ""
    except Exception as e:
        state["status"] = f"❌ RTL generation failed: {e}"
        return state

    raw_output_path = os.path.join(workflow_dir, "rtl_llm_raw_output.txt")
    with open(raw_output_path, "w", encoding="utf-8") as f:
        f.write(llm_output)

    verilog_map = _parse_named_verilog_blocks(llm_output)
    if not verilog_map:
        raise ValueError("Expected named Verilog blocks in RTL agent output.")

    expected_files = _collect_expected_rtl_files(spec_json, mode)
    artifact_list = []

    for fname in expected_files:
        code = verilog_map.get(fname)
        if not code:
            continue
        fpath = os.path.join(workflow_dir, fname)
        with open(fpath, "w", encoding="utf-8") as vf:
            vf.write(code + "\n")
        artifact_list.append(fpath)

    issues, clock_ports, reset_ports = _validate_spec_vs_rtl(spec_json, mode, verilog_map)

    top_rtl_file = _top_rtl_file(spec_json, mode)
    top_rtl_path = os.path.join(workflow_dir, top_rtl_file)

    log_path = os.path.join(workflow_dir, "rtl_agent_compile.log")
    compile_status = "✅ Verilog syntax check passed."

    if not os.path.exists(top_rtl_path):
        issues.append(f"❌ Top RTL file missing after generation: {top_rtl_file}")
    if not artifact_list:
        issues.append("❌ No RTL files materialized to disk.")

    if not issues:
        try:
            iverilog = os.getenv("IVERILOG_BIN", "iverilog")
            compile_cmd = [iverilog, "-o", os.path.join(workflow_dir, "rtl_check.out")] + artifact_list

            result = subprocess.run(
                compile_cmd,
                check=True,
                capture_output=True,
                text=True
            )

            with open(log_path, "w", encoding="utf-8") as logf:
                logf.write(f"RTL Compile Log — {datetime.datetime.now()}\n\n")
                logf.write("Compile status: PASS\n")
                if result.stdout:
                    logf.write("\nSTDOUT:\n")
                    logf.write(result.stdout)
                if result.stderr:
                    logf.write("\nSTDERR:\n")
                    logf.write(result.stderr)

        except subprocess.CalledProcessError as e:
            compile_status = "⚠ Verilog syntax check failed."
            err = (e.stderr or e.stdout or "").strip()
            with open(log_path, "w", encoding="utf-8") as logf:
                logf.write(f"RTL Compile Log — {datetime.datetime.now()}\n\n")
                logf.write("Compile status: FAIL\n\n")
                logf.write(err)
            issues.append(f"❌ Compile/elaboration failed: {err}")
    else:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"RTL Compile Log — {datetime.datetime.now()}\n\n")
            logf.write("Compile skipped due to earlier contract validation issues.\n")
            for i in issues:
                logf.write(f"- {i}\n")

    summary_file = os.path.join(workflow_dir, "rtl_agent_summary.txt")
    overall_status = "✅ RTL generated and validated successfully." if not issues else "⚠ RTL generation completed with issues."
    with open(summary_file, "w", encoding="utf-8") as sf:
        sf.write(f"{overall_status}\n\n")
        sf.write(f"Spec mode: {mode}\n")
        sf.write(f"Top module: {_top_module_name(spec_json, mode)}\n")
        sf.write(f"{compile_status}\n\n")
        sf.write(f"Generated files: {[os.path.basename(x) for x in artifact_list]}\n")
        sf.write(f"Clock ports: {clock_ports}\n")
        sf.write(f"Reset ports: {reset_ports}\n")
        sf.write("Issues:\n")
        if issues:
            for i in issues:
                sf.write(f" - {i}\n")
        else:
            sf.write(" - None\n")

    try:
        with open(log_path, "r", encoding="utf-8") as lf:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="rtl",
                filename="rtl_agent_compile.log",
                content=lf.read(),
            )

        with open(summary_file, "r", encoding="utf-8") as sf:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="rtl",
                filename="rtl_agent_summary.txt",
                content=sf.read(),
            )

        with open(raw_output_path, "r", encoding="utf-8") as rf:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="rtl",
                filename="rtl_llm_raw_output.txt",
                content=rf.read(),
            )

        for fpath in artifact_list:
            with open(fpath, "r", encoding="utf-8") as vf:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="rtl",
                    filename=os.path.basename(fpath),
                    content=vf.read(),
                )
    except Exception as e:
        print(f"⚠️ RTL Agent artifact upload failed: {e}")

    state.update({
        "status": overall_status,
        "artifact": top_rtl_path if os.path.exists(top_rtl_path) else (artifact_list[0] if artifact_list else None),
        "artifact_list": artifact_list,
        "artifact_log": log_path,
        "port_list": sorted(set(clock_ports + reset_ports)),
        "clock_ports": clock_ports,
        "reset_ports": reset_ports,
        "issues": issues,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })

    return state