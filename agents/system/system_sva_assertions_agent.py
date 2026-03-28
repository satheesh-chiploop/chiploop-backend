"""
ChipLoop Verification & Validation - System Assertions Agent

Design goals:
- system_integration_intent.json is the primary source of truth for system-level verification intent
- soc_top_sim.sv is used to recover exact top-level port widths/signatures when available
- this agent is dedicated to system-level runs (no digital/system mode switching)
- generated SVA remains lightweight, simulation-friendly, and generic
- no hardcoded protocol-specific signal assumptions in generated assertions
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

client_openai = OpenAI() if OpenAI else None
logger = logging.getLogger("chiploop")


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str, level: str = "info") -> None:
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] [{level.upper()}] {msg}\n")



def _log_kv(path: str, key: str, value: Any, level: str = "info") -> None:
    try:
        rendered = json.dumps(value, indent=2, default=str)
    except Exception:
        rendered = str(value)
    _log(path, f"{key}={rendered}", level=level)



def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}



def _safe_read_text(path: Optional[str]) -> str:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""



def _safe_read_lines(path: Optional[str]) -> List[str]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    return []



def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir



def _record_text(
    workflow_id: str,
    agent_name: str,
    subdir: str,
    filename: str,
    content: str,
) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None



def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# -----------------------------------------------------------------------------
# Artifact discovery
# -----------------------------------------------------------------------------

def _find_system_integration_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json"),
        os.path.join(workflow_dir, "system_integration_intent.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_soc_top_sim_path(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "soc_top_sim.sv"),
        os.path.join(workflow_dir, "soc_top_sim.sv"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_system_rtl_filelist(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_rtl_filelist_sim.txt"),
        os.path.join(workflow_dir, "system_rtl_filelist_sim.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _find_regmap_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "digital", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "digital_regmap.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None



def _collect_rtl_files_from_filelist(path: Optional[str]) -> List[str]:
    files: List[str] = []
    for ln in _safe_read_lines(path):
        if not isinstance(ln, str):
            continue
        p = ln.strip()
        if not p:
            continue
        files.append(os.path.abspath(p))
    return list(dict.fromkeys(files))



def _collect_system_rtl_files(workflow_dir: str) -> List[str]:
    filelist = _find_system_rtl_filelist(workflow_dir)
    if filelist:
        files = _collect_rtl_files_from_filelist(filelist)
        if files:
            return files

    exts = (".v", ".sv", ".vh", ".svh")
    scan_dirs = [
        os.path.join(workflow_dir, "system", "integration"),
        os.path.join(workflow_dir, "digital", "rtl_refactored"),
        os.path.join(workflow_dir, "analog"),
    ]
    out: List[str] = []
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    out.append(os.path.abspath(os.path.join(root, fn)))
    return sorted(set(out))


# -----------------------------------------------------------------------------
# Top/port extraction
# -----------------------------------------------------------------------------

def _normalize_direction(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("input", "in", "i"):
        return "input"
    if s in ("output", "out", "o"):
        return "output"
    if s in ("inout", "io"):
        return "inout"
    return s



def _normalize_width_token(width: Any) -> str:
    if width is None:
        return "1"
    s = str(width).strip()
    if not s:
        return "1"
    if s.startswith("[") and s.endswith("]"):
        return s
    if s == "1":
        return "1"
    return s



def _port_decl_for_logic(name: str, width: Any) -> str:
    w = _normalize_width_token(width)
    if w == "1":
        return f"  input logic {name}"
    if w.startswith("[") and w.endswith("]"):
        return f"  input logic {w} {name}"
    return f"  input logic [{w}-1:0] {name}"



def _parse_top_module_name_from_sv(text: str) -> Optional[str]:
    m = re.search(r"\bmodule\s+([a-zA-Z_][a-zA-Z0-9_$]*)\s*\(", text)
    if m:
        return m.group(1)
    return None



def _ports_from_top_sv(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not text.strip():
        return out

    m = re.search(
        r"\bmodule\s+[a-zA-Z_][a-zA-Z0-9_$]*\s*\((.*?)\)\s*;",
        text,
        flags=re.DOTALL,
    )
    if not m:
        return out

    header = m.group(1)
    for raw_line in header.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue

        pm = re.match(
            r"^(input|output|inout)\s+(?:wire\s+|logic\s+|reg\s+)?(?:signed\s+)?(?:([^\s]+)\s+)?([a-zA-Z_][a-zA-Z0-9_$]*)$",
            line,
            flags=re.IGNORECASE,
        )
        if not pm:
            continue

        direction = _normalize_direction(pm.group(1))
        width = pm.group(2).strip() if pm.group(2) else "1"
        name = pm.group(3)
        out.append({"name": name, "direction": direction, "width": width})

    return out



def _ports_from_integration_json(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    ports: Dict[str, Dict[str, Any]] = {}
    for c in intent.get("connections", []):
        if not isinstance(c, dict):
            continue
        src = str(c.get("from", "")).strip()
        dst = str(c.get("to", "")).strip()

        if src.startswith("top."):
            nm = src.split(".", 1)[1].split("[", 1)[0]
            ports[nm] = {"name": nm, "direction": "input", "width": "1"}
        if dst.startswith("top."):
            nm = dst.split(".", 1)[1].split("[", 1)[0]
            ports[nm] = {"name": nm, "direction": "output", "width": "1"}
    return [ports[k] for k in sorted(ports.keys())]



def _merge_ports(primary: List[Dict[str, Any]], fallback: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for p in fallback:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        merged[str(p["name"])] = dict(p)

    for p in primary:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        merged[str(p["name"])] = dict(p)

    return [merged[k] for k in sorted(merged.keys())]



def _infer_clocks_resets(ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    for p in ports:
        nm = str(p.get("name", ""))
        if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
            clocks.append(nm)
        if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
            resets.append(
                {
                    "name": nm,
                    "active_low": bool(re.search(r"(rst_n|reset_n|por_n)", nm, re.IGNORECASE)),
                    "async": False,
                }
            )

    clocks = [c for c in clocks if c.strip()]
    clocks = list(dict.fromkeys(clocks))

    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for r in resets:
        nm = str(r.get("name", "")).strip()
        if nm and nm not in seen:
            seen.add(nm)
            uniq_resets.append(r)

    return clocks, uniq_resets


# -----------------------------------------------------------------------------
# Contract resolution
# -----------------------------------------------------------------------------

def _resolve_system_sva_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    integration_json_path = (
        state.get("system_integration_intent_json")
        or _find_system_integration_json(workflow_dir)
    )
    soc_top_sim_path = (
        state.get("soc_top_sim_path")
        or state.get("soc_top_path")
        or _find_soc_top_sim_path(workflow_dir)
    )
    regmap_json_path = (
        state.get("digital_regmap_json")
        or state.get("regmap_json")
        or _find_regmap_json(workflow_dir)
    )
    rtl_filelist_path = (
        state.get("system_rtl_filelist_sim")
        or _find_system_rtl_filelist(workflow_dir)
    )

    filelist_value = state.get("system_rtl_filelist_sim")
    rtl_files = []
    rtl_source = ""

    if isinstance(filelist_value, list) and filelist_value:
        rtl_files = [os.path.abspath(p) for p in filelist_value if isinstance(p, str) and p.strip()]
        rtl_source = "state.system_rtl_filelist_sim"
    elif isinstance(filelist_value, str) and filelist_value.strip():
        rtl_files = _collect_rtl_files_from_filelist(filelist_value)
        rtl_source = "state.system_rtl_filelist_sim"

    if not rtl_files and rtl_filelist_path:
        rtl_files = _collect_rtl_files_from_filelist(rtl_filelist_path)
        rtl_source = "system_rtl_filelist_sim"

    if not rtl_files:
        rtl_files = _collect_system_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"

    rtl_files = [os.path.abspath(p) for p in rtl_files if isinstance(p, str)]

    integration = _safe_read_json(integration_json_path)
    soc_top_text = _safe_read_text(soc_top_sim_path)

    top_module = (
        state.get("soc_top_sim_module")
        or state.get("soc_top_name")
        or ((integration.get("top") or {}).get("sim_module") if isinstance(integration.get("top"), dict) else None)
        or _parse_top_module_name_from_sv(soc_top_text)
        or "soc_top_sim"
    )

    ports_from_sv = _ports_from_top_sv(soc_top_text)
    ports_from_intent = _ports_from_integration_json(integration)
    ports = _merge_ports(ports_from_sv, ports_from_intent)
    clocks, resets = _infer_clocks_resets(ports)

    contract = {
        "integration_json_path": integration_json_path,
        "soc_top_sim_path": soc_top_sim_path,
        "regmap_json_path": regmap_json_path,
        "rtl_filelist_path": rtl_filelist_path,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top_module,
        "ports": ports,
        "clock_names": clocks,
        "reset_signals": resets,
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "integration_json_path": contract["integration_json_path"],
            "soc_top_sim_path": contract["soc_top_sim_path"],
            "regmap_json_path": contract["regmap_json_path"],
            "rtl_filelist_path": contract["rtl_filelist_path"],
            "rtl_source": contract["rtl_source"],
            "rtl_file_count": len(contract["rtl_files"]),
            "top_module": contract["top_module"],
            "port_count": len(contract["ports"]),
            "clock_names": contract["clock_names"],
            "reset_names": [r.get("name") for r in contract["reset_signals"]],
        },
    )
    return contract


# -----------------------------------------------------------------------------
# Spec construction
# -----------------------------------------------------------------------------

def _build_sva_spec(
    top_module: str,
    ports: List[Dict[str, Any]],
    clock_names: List[str],
    reset_signals: List[Dict[str, Any]],
    integration: Dict[str, Any],
    regmap: Dict[str, Any],
) -> Dict[str, Any]:
    port_points: List[Dict[str, Any]] = []
    for p in ports:
        name = p.get("name")
        if not name:
            continue
        port_points.append(
            {
                "name": str(name),
                "direction": _normalize_direction(p.get("direction")),
                "width": _normalize_width_token(p.get("width", "1")),
            }
        )

    reg_block = regmap.get("regmap") if isinstance(regmap, dict) else {}
    reg_names: List[str] = []
    if isinstance(reg_block, dict):
        regs = reg_block.get("registers")
        if isinstance(regs, list):
            for r in regs:
                if isinstance(r, dict) and r.get("name"):
                    reg_names.append(str(r["name"]))

    return {
        "top_module": top_module,
        "clock_names": list(clock_names),
        "reset_signals": list(reset_signals),
        "ports": port_points,
        "integration_summary": {
            "instance_names": [inst.get("name") for inst in integration.get("instances", []) if isinstance(inst, dict) and inst.get("name")],
            "connection_count": len(integration.get("connections", []) if isinstance(integration.get("connections", []), list) else []),
        },
        "control_model": {
            "has_regmap": bool(regmap),
            "reg_count": len(reg_names),
            "register_names": reg_names,
        },
    }


# -----------------------------------------------------------------------------
# SVA generation
# -----------------------------------------------------------------------------

def _default_sva_module(module_name: str, sva_spec: Dict[str, Any]) -> str:
    clocks = list(sva_spec.get("clock_names", []))
    resets = list(sva_spec.get("reset_signals", []))
    ports = list(sva_spec.get("ports", []))

    primary_clock = clocks[0] if clocks else None
    primary_reset = resets[0]["name"] if resets else None
    primary_reset_active_low = bool(resets[0].get("active_low", False)) if resets else False

    module_ports: List[str] = []
    for p in ports:
        nm = str(p.get("name", "")).strip()
        if not nm:
            continue
        module_ports.append(_port_decl_for_logic(nm, p.get("width", "1")))

    if not module_ports:
        module_ports.append("  input logic dummy_clk")

    clocking_expr = primary_clock if primary_clock else "dummy_clk"
    if primary_reset:
        disable_iff = f"disable iff ({'!' if primary_reset_active_low else ''}{primary_reset})"
        reset_known_expr = primary_reset
    else:
        disable_iff = ""
        reset_known_expr = None

    outputs = [p for p in ports if p.get("direction") == "output"]
    inputs = [p for p in ports if p.get("direction") == "input"]

    prop_blocks: List[str] = []

    if primary_reset and primary_clock:
        prop_blocks.append(
            f"""  property p_reset_known;
    @(posedge {clocking_expr})
      !$isunknown({reset_known_expr});
  endproperty

  a_reset_known: assert property(p_reset_known)
    else $error(\"Reset signal has X/Z state.\");
"""
        )

    for p in outputs[:16]:
        nm = str(p["name"])
        if not primary_clock:
            continue
        if disable_iff:
            body = f"""  property p_{nm}_known_after_reset;
    @(posedge {clocking_expr}) {disable_iff}
      !$isunknown({nm});
  endproperty

  a_{nm}_known_after_reset: assert property(p_{nm}_known_after_reset)
    else $error(\"Top-level output {nm} has X/Z after reset release.\");
"""
        else:
            body = f"""  property p_{nm}_known;
    @(posedge {clocking_expr})
      !$isunknown({nm});
  endproperty

  a_{nm}_known: assert property(p_{nm}_known)
    else $error(\"Top-level output {nm} has X/Z.\");
"""
        prop_blocks.append(body)

    # Generic stability check for outputs that look like status/irq/fault/ready indicators.
    indicator_outputs = [
        str(p["name"])
        for p in outputs
        if re.search(r"(irq|fault|ready|err|done|valid)", str(p.get("name", "")), re.IGNORECASE)
    ]
    for nm in indicator_outputs[:8]:
        if primary_clock and disable_iff:
            prop_blocks.append(
                f"""  property p_{nm}_single_bit_known;
    @(posedge {clocking_expr}) {disable_iff}
      !$isunknown({nm}[0]);
  endproperty

  a_{nm}_single_bit_known: assert property(p_{nm}_single_bit_known)
    else $error(\"Indicator output {nm} has X/Z after reset release.\");
"""
            )

    # Lightweight assumption-free input sanity comments derived from inputs.
    if not prop_blocks:
        prop_blocks.append(
            """  // No clock/reset/output-derived assertions were generated from system artifacts.
  // Extend this scaffold using only ports declared in soc_top_sim.sv / integration intent.
"""
        )

    joined_ports = ",\n".join(module_ports)
    input_names = ", ".join(str(p.get("name")) for p in inputs[:12])
    output_names = ", ".join(str(p.get("name")) for p in outputs[:12])

    return f"""/*
 * Auto-generated system-level SVA scaffold.
 * Derived from system integration intent and top-level simulation module.
 * This module observes only top-level signals and does not invent internal hierarchy.
 * Input sample: {input_names if input_names else 'n/a'}
 * Output sample: {output_names if output_names else 'n/a'}
 */

module {module_name} (
{joined_ports}
);

{''.join(prop_blocks)}endmodule
"""



def _maybe_llm_expand(
    integration: Dict[str, Any],
    regmap: Dict[str, Any],
    soc_top_text: str,
    sva: str,
    log_path: str,
    sva_spec: Dict[str, Any],
) -> str:
    if not client_openai:
        _log(log_path, "LLM expansion unavailable; using deterministic scaffold only.", level="warning")
        return sva

    try:
        prompt = (
            "You are a senior RTL verification engineer.\n"
            "Expand this system-level SVA scaffold conservatively.\n"
            "Constraints:\n"
            "- Use ONLY signal names present verbatim in SVA_SPEC or SOC_TOP_SIM.\n"
            "- Do NOT invent internal nets, module hierarchy, protocols, or interfaces.\n"
            "- Keep the module name and port list intact.\n"
            "- Prefer top-level reset/X-checking and generic safety properties.\n"
            "- Return SystemVerilog code only. No markdown.\n\n"
            f"SVA_SPEC:\n{json.dumps(sva_spec, indent=2)}\n\n"
            f"INTEGRATION_JSON:\n{json.dumps(integration, indent=2)}\n\n"
            f"REGMAP_JSON:\n{json.dumps(regmap, indent=2)}\n\n"
            f"SOC_TOP_SIM:\n{soc_top_text}\n\n"
            f"SVA_CODE:\n{sva}\n"
        )
        resp = client_openai.chat.completions.create(
            model=os.getenv("SYSTEM_SVA_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Return code only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        out = resp.choices[0].message.content.strip()
        if out:
            _log(log_path, "LLM expansion completed.")
            return out
    except Exception as e:
        _log(log_path, f"LLM expansion skipped/failed: {e}", level="warning")

    return sva



def _gen_bind_sv(top: str, module_name: str, sva_spec: Dict[str, Any]) -> str:
    conns: List[str] = []
    for p in sva_spec.get("ports", []):
        nm = p.get("name")
        if nm:
            conns.append(f"  .{nm}({nm})")
    joined = ",\n".join(conns)
    return f"""/*
 * Auto-generated system SVA bind file.
 * Uses only top-level signals declared in the resolved system contract.
 */
bind {top} {module_name} u_{module_name} (
{joined}
);
"""


# -----------------------------------------------------------------------------
# Agent entrypoint
# -----------------------------------------------------------------------------

def run_agent(state: dict) -> dict:
    agent_name = "System Assertions Agent"
    artifacts: Dict[str, Any] = {}

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "system_sva_assertions_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System Assertions Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    contract = _resolve_system_sva_contract(state, workflow_dir, log_path)
    integration_json_path = contract["integration_json_path"]
    soc_top_sim_path = contract["soc_top_sim_path"]
    regmap_json_path = contract["regmap_json_path"]
    rtl_files = contract["rtl_files"]
    top = contract["top_module"]
    ports = contract["ports"]
    clocks = contract["clock_names"]
    resets = contract["reset_signals"]

    integration = _safe_read_json(integration_json_path)
    regmap = _safe_read_json(regmap_json_path)
    soc_top_text = _safe_read_text(soc_top_sim_path)
    sva_spec = _build_sva_spec(top, ports, clocks, resets, integration, regmap)

    _log(log_path, f"integration_json_path={integration_json_path}")
    _log(log_path, f"soc_top_sim_path={soc_top_sim_path}")
    _log(log_path, f"regmap_json_path={regmap_json_path}")
    _log(log_path, f"top_module={top}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log_kv(log_path, "clock_candidates", clocks)
    _log_kv(log_path, "reset_candidates", resets)
    _log_kv(
        log_path,
        "sva_ports",
        {
            "port_names": [p["name"] for p in sva_spec["ports"]],
            "input_count": len([p for p in sva_spec["ports"] if p["direction"] == "input"]),
            "output_count": len([p for p in sva_spec["ports"] if p["direction"] == "output"]),
            "widths": {p["name"]: p.get("width", "1") for p in sva_spec["ports"]},
        },
    )

    out_dir = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(out_dir, exist_ok=True)

    module_name = f"{top}_assertions"
    sva_sv = _default_sva_module(module_name, sva_spec)

    enable_llm_expand = str(os.getenv("CHIPLOOP_ENABLE_LLM_SVA_EXPAND", "0")).strip().lower() in ("1", "true", "yes")
    if enable_llm_expand:
        sva_sv = _maybe_llm_expand(integration, regmap, soc_top_text, sva_sv, log_path, sva_spec)
    else:
        _log(log_path, "LLM SVA expansion disabled; using deterministic scaffold.")

    bind_sv = _gen_bind_sv(top, module_name, sva_spec)

    bind_readme = f"""# System SVA Usage

Generated:
- `{module_name}.sv`        : assertion module derived from system integration/top-level contract
- `{module_name}_bind.sv`   : bind file for DUT integration
- `sva_spec.json`           : resolved system assertion contract
- `sva_generation_report.json`

Primary sources used:
- system integration intent
- system top simulation module
- digital register map (when present, for metadata/reporting only)

The bind file uses only resolved top-level signals and is intended to be compiled with simulation sources.
"""

    _write_file(os.path.join(out_dir, f"{module_name}.sv"), sva_sv)
    _write_file(os.path.join(out_dir, f"{module_name}_bind.sv"), bind_sv)
    _write_file(os.path.join(out_dir, "sva_spec.json"), json.dumps(sva_spec, indent=2))
    _write_file(os.path.join(out_dir, "SVA_README.md"), bind_readme)

    artifacts["sva_sv"] = _record_text(workflow_id, agent_name, "vv/tb", f"{module_name}.sv", sva_sv)
    artifacts["sva_bind_sv"] = _record_text(workflow_id, agent_name, "vv/tb", f"{module_name}_bind.sv", bind_sv)
    artifacts["sva_spec_json"] = _record_text(workflow_id, agent_name, "vv/tb", "sva_spec.json", json.dumps(sva_spec, indent=2))
    artifacts["sva_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "SVA_README.md", bind_readme)

    primary_reset = resets[0]["name"] if resets else None
    primary_reset_active_low = bool(resets[0].get("active_low", False)) if resets else False
    primary_clock = clocks[0] if clocks else None

    report = {
        "type": "system_sva_generation",
        "version": "1.0",
        "top_module": top,
        "integration_json_path": integration_json_path,
        "soc_top_sim_path": soc_top_sim_path,
        "regmap_json_path": regmap_json_path,
        "rtl_file_count": len(rtl_files),
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "primary_clock": primary_clock,
        "primary_reset": primary_reset,
        "primary_reset_active_low": primary_reset_active_low,
        "generated_dir": "vv/tb",
        "sva_module_name": module_name,
        "sva_bind_file": f"{module_name}_bind.sv",
        "assertion_output_signals": [p["name"] for p in sva_spec["ports"] if p["direction"] == "output"][:16],
        "artifacts": artifacts,
    }

    if primary_reset and re.search(r"(?:^|_)(rst_n|reset_n|por_n)(?:$|_)", primary_reset, re.IGNORECASE) and not primary_reset_active_low:
        _log(log_path, f"Reset name suggests active-low but resolved active_low=False: {primary_reset}", level="warning")

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(out_dir, "sva_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "sva_generation_report.json", rep_txt)

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
    except Exception:
        log_text = ""
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "system_sva_assertions_agent.log", log_text)

    state.setdefault("vv", {})
    state["vv"]["sva"] = report
    state["vv"]["sva_spec"] = sva_spec

    state["sva_assertions_path"] = os.path.join(out_dir, f"{module_name}.sv")
    state["sva_spec_json"] = os.path.join(out_dir, "sva_spec.json")
    state["sva_bind_path"] = os.path.join(out_dir, f"{module_name}_bind.sv")

    _log(log_path, f"{agent_name} completed successfully.")
    return state
