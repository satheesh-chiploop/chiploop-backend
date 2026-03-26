"""
ChipLoop Verification & Validation - Digital SVA Assertions Agent

Design goals:
- spec_json / digital_spec_json is the primary source of truth
- no hardcoded DUT signal names in the generated scaffold
- support both digital-only and future system/SoC runs
- log decisions to backend logger and persistent artifact log
- generate lightweight, simulation-friendly SVA collateral
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



def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")

    handoff_dirs = [
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "rtl"),
        os.path.join(workflow_dir, "handoff", "rtl"),
    ]

    for d in handoff_dirs:
        if not os.path.isdir(d):
            continue

        rtl: List[str] = []
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))

        rtl = sorted(set(rtl))
        if rtl:
            return rtl

    return []


def _find_fallback_spec_json(workflow_dir: str) -> Optional[str]:
    preferred: List[str] = []
    fallback: List[str] = []

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            if not fn.endswith("_spec.json") and "spec" not in fn.lower():
                continue

            path = os.path.join(root, fn)
            norm = path.replace("\\", "/").lower()
            if "/digital/" in norm:
                preferred.append(path)
            elif "/analog/" in norm:
                continue
            else:
                fallback.append(path)

    if preferred:
        preferred.sort()
        return preferred[0]
    if fallback:
        fallback.sort()
        return fallback[0]
    return None


def _pick_top_module(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()

    hierarchy = spec.get("hierarchy") or {}
    top2 = hierarchy.get("top_module")
    if isinstance(top2, dict):
        nm = top2.get("name")
        if isinstance(nm, str) and nm.strip():
            return nm.strip()
    elif isinstance(top2, str) and top2.strip():
        return top2.strip()

    if state_top and isinstance(state_top, str) and state_top.strip():
        return state_top.strip()

    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue

    return "top"


def _ports_from_spec(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    tm = spec.get("top_module") or {}
    ports = tm.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    hierarchy = spec.get("hierarchy") or {}
    htop = hierarchy.get("top_module")
    if isinstance(htop, dict):
        ports = htop.get("ports")
        if isinstance(ports, list) and ports:
            return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    ports = spec.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    io = spec.get("io")
    if isinstance(io, dict):
        for dkey, direction in [("inputs", "input"), ("outputs", "output"), ("inouts", "inout")]:
            arr = io.get(dkey)
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, dict) and p.get("name"):
                        q = dict(p)
                        q.setdefault("direction", direction)
                        out.append(q)

    return out


def _normalize_direction(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("input", "in", "i"):
        return "input"
    if s in ("output", "out", "o"):
        return "output"
    if s in ("inout", "io"):
        return "inout"
    return s


def _infer_clocks_resets(spec: Dict[str, Any], ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    clk_spec = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks")
    if isinstance(clk_spec, list):
        for c in clk_spec:
            if isinstance(c, dict) and c.get("name"):
                clocks.append(str(c["name"]))
            elif isinstance(c, str):
                clocks.append(c)

    rst_spec = spec.get("resets") or (spec.get("reset") or {})
    if isinstance(rst_spec, list):
        for r in rst_spec:
            if isinstance(r, dict) and r.get("name"):
                resets.append(dict(r))
            elif isinstance(r, str):
                resets.append({"name": r})
    elif isinstance(rst_spec, dict) and rst_spec.get("name"):
        resets.append(dict(rst_spec))

    if not clocks:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
                clocks.append(nm)

    if not resets:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
                resets.append({"name": nm})


    clocks = [c for c in clocks if isinstance(c, str) and c.strip()]
    clocks = list(dict.fromkeys(clocks))

    norm_resets: List[Dict[str, Any]] = []
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            nm = str(r.get("name"))
            is_name_active_low = bool(re.search(r"(rst_n|reset_n|por_n)", nm, re.IGNORECASE))

            rr = {
                "name": nm,
                "active_low": bool(
                    r.get("active_low", False)
                    or is_name_active_low
                    or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")
                ),
                "async": bool(
                    str(r.get("type", "")).lower() in ("async", "asynchronous")
                    or r.get("async", False)
                ),
            }
            norm_resets.append(rr)

    # remove duplicates
    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for rr in norm_resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq_resets.append(rr)

    return clocks, uniq_resets



def _resolve_sva_mode(state: Dict[str, Any]) -> str:
    if (
        state.get("soc_top_sim_module")
        or state.get("soc_top_name")
        or state.get("system_integration_intent_json")
        or state.get("soc_top_sim_path")
    ):
        return "system"
    return "digital"


def _resolve_sva_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    mode = _resolve_sva_mode(state)

    spec_path = state.get("spec_json") or state.get("digital_spec_json")
    spec_source = "state"
    if not spec_path:
        spec_path = _find_fallback_spec_json(workflow_dir)
        spec_source = "fallback_scan"

    spec = _safe_read_json(spec_path)

    rtl_files = state.get("rtl_files")
    rtl_source = "state.rtl_files"
    if not isinstance(rtl_files, list) or not rtl_files:
        rtl_files = _collect_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"

    rtl_files = [os.path.abspath(p) for p in rtl_files if isinstance(p, str)]

    if mode == "system":
        top = (
            state.get("soc_top_sim_module")
            or state.get("soc_top_name")
            or _pick_top_module(spec, rtl_files, state.get("top_module"))
        )
    else:
        top = state.get("top_module") or _pick_top_module(spec, rtl_files, state.get("top_module"))

    contract = {
        "mode": mode,
        "spec_path": spec_path,
        "spec_source": spec_source,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top,
        "soc_mode": bool(mode == "system"),
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "mode": contract["mode"],
            "spec_path": contract["spec_path"],
            "spec_source": contract["spec_source"],
            "rtl_source": contract["rtl_source"],
            "rtl_file_count": len(contract["rtl_files"]),
            "top_module": contract["top_module"],
            "soc_mode": contract["soc_mode"],
        },
    )
    return contract


def _build_sva_spec(spec: Dict[str, Any], top: str, soc_mode: bool = False) -> Dict[str, Any]:
    ports = [] if soc_mode else _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)

    port_points: List[Dict[str, Any]] = []
    for p in ports:
        name = p.get("name")
        if not name:
            continue
        port_points.append(
            {
                "name": str(name),
                "direction": _normalize_direction(p.get("direction")),
            }
        )

    return {
        "top_module": top,
        "soc_mode": soc_mode,
        "clock_names": clocks,
        "reset_signals": resets,
        "ports": port_points,
    }


def _default_sva_module(module_name: str, sva_spec: Dict[str, Any]) -> str:
    clocks = list(sva_spec.get("clock_names", []))
    resets = list(sva_spec.get("reset_signals", []))
    ports = list(sva_spec.get("ports", []))

    primary_clock = clocks[0] if clocks else None
    primary_reset = resets[0]["name"] if resets else None
    primary_reset_active_low = bool(resets[0].get("active_low", False)) if resets else False

    inputs = [p["name"] for p in ports if p.get("direction") == "input"]
    outputs = [p["name"] for p in ports if p.get("direction") == "output"]

    module_ports: List[str] = []

    all_ports = set()

    if primary_clock:
        all_ports.add(primary_clock)
    if primary_reset:
        all_ports.add(primary_reset)

    for nm in inputs:
        all_ports.add(nm)
    for nm in outputs:
        all_ports.add(nm)

    module_ports = [f"  input logic {nm}" for nm in sorted(all_ports)]
    
    if not module_ports:
        module_ports.append("  input logic dummy_clk")

    clocking_expr = primary_clock if primary_clock else "dummy_clk"

    if primary_reset:
        disable_iff = f"disable iff ({'!' if primary_reset_active_low else ''}{primary_reset})"
        reset_known_expr = primary_reset
    else:
        disable_iff = ""
        reset_known_expr = None

    prop_blocks: List[str] = []

    if primary_reset and primary_clock:
        prop_blocks.append(
            f"""  property p_reset_known;
    @(posedge {clocking_expr})
      !$isunknown({reset_known_expr});
  endproperty

  a_reset_known: assert property(p_reset_known)
    else $error("Reset signal has X/Z state.");
"""
        )

    for nm in outputs[:12]:
        if not primary_clock:
            continue
        if disable_iff:
            body = f"""  property p_{nm}_known_after_reset;
    @(posedge {clocking_expr}) {disable_iff}
      !$isunknown({nm});
  endproperty

  a_{nm}_known_after_reset: assert property(p_{nm}_known_after_reset)
    else $error("Signal {nm} has X/Z after reset release.");
"""
        else:
            body = f"""  property p_{nm}_known;
    @(posedge {clocking_expr})
      !$isunknown({nm});
  endproperty

  a_{nm}_known: assert property(p_{nm}_known)
    else $error("Signal {nm} has X/Z.");
"""
        prop_blocks.append(body)

    if not prop_blocks:
        prop_blocks.append(
            """  // No clock/reset/output-derived assertions were generated from spec.
  // Extend this scaffold using only signals declared in spec_json.
"""
        )

    joined_ports = ",\n".join(module_ports)

    return f"""/*
 * Auto-generated SVA scaffold.
 * Derived from spec_json / digital_spec_json.
 * No hardcoded design-specific signal assumptions.
 */

module {module_name} (
{joined_ports}
);

{''.join(prop_blocks)}
endmodule
"""


def _maybe_llm_expand(spec: Dict[str, Any], sva: str, log_path: str, sva_spec: Dict[str, Any]) -> str:
    if not client_openai:
        _log(log_path, "LLM expansion unavailable; using deterministic scaffold only.", level="warning")
        return sva

    try:
        prompt = (
            "You are a senior RTL verification engineer.\n"
            "Expand this SVA scaffold conservatively.\n"
            "Constraints:\n"
            "- Use ONLY signal names present verbatim in SVA_SPEC or SPEC_JSON.\n"
            "- Do NOT invent any signal names, buses, protocols, or interfaces.\n"
            "- Keep the module name and port list intact.\n"
            "- Prefer simple reset/X-checking and generic safety properties.\n"
            "- Return SystemVerilog code only. No markdown.\n\n"
            f"SVA_SPEC:\n{json.dumps(sva_spec, indent=2)}\n\n"
            f"SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"SVA_CODE:\n{sva}\n"
        )
        resp = client_openai.chat.completions.create(
            model=os.getenv("DIGITAL_SVA_MODEL", "gpt-4o-mini"),
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
 * Auto-generated SVA bind file.
 * Uses only spec-declared signals.
 */
bind {top} {module_name} u_{module_name} (
{joined}
);
"""


def run_agent(state: dict) -> dict:
    agent_name = "Digital Assertions (SVA) Agent"

    artifacts: Dict[str, Any] = {}

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "digital_sva_assertions_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Digital Assertions (SVA) Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

 

    contract = _resolve_sva_contract(state, workflow_dir, log_path)
    mode = contract["mode"]
    spec_path = contract["spec_path"]
    rtl_files = contract["rtl_files"]
    top = contract["top_module"]
    soc_mode = contract["soc_mode"]

    spec = _safe_read_json(spec_path)
    ports = [] if soc_mode else _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)
    sva_spec = _build_sva_spec(spec, top, soc_mode=soc_mode)

    _log(log_path, f"resolved_mode={mode}")
    _log(log_path, f"spec_path={spec_path}")
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
        },
    )

    out_dir = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(out_dir, exist_ok=True)

    module_name = f"{top}_assertions"


    sva_sv = _default_sva_module(module_name, sva_spec)

    enable_llm_expand = str(os.getenv("CHIPLOOP_ENABLE_LLM_SVA_EXPAND", "0")).strip().lower() in ("1", "true", "yes")
    if enable_llm_expand:
        sva_sv = _maybe_llm_expand(spec, sva_sv, log_path, sva_spec)
    else:
        _log(log_path, "LLM SVA expansion disabled; using deterministic scaffold.")

    bind_sv = _gen_bind_sv(top, module_name, sva_spec)

    bind_readme = f"""# SVA Usage

Generated:
- `{module_name}.sv`        : assertion module derived from spec
- `{module_name}_bind.sv`   : bind file for DUT integration
- `sva_spec.json`           : resolved assertion contract
- `sva_generation_report.json`

The bind file uses only spec-declared signals and is intended to be compiled with simulation sources.
"""

    _write_file(os.path.join(out_dir, f"{module_name}_bind.sv"), bind_sv)
    artifacts["sva_bind_sv"] = _record_text(
        workflow_id, agent_name, "vv/tb", f"{module_name}_bind.sv", bind_sv
    )

    state["sva_bind_path"] = os.path.join(out_dir, f"{module_name}_bind.sv")

    sva_spec_txt = json.dumps(sva_spec, indent=2)

    _write_file(os.path.join(out_dir, f"{module_name}.sv"), sva_sv)
    _write_file(os.path.join(out_dir, "sva_spec.json"), sva_spec_txt)
    _write_file(os.path.join(out_dir, "SVA_README.md"), bind_readme)

    _log(log_path, f"Generated {module_name}.sv")
    _log(log_path, "Generated sva_spec.json")
    _log(log_path, "Generated SVA_README.md")


    artifacts["sva_sv"] = _record_text(workflow_id, agent_name, "vv/tb", f"{module_name}.sv", sva_sv)
    artifacts["sva_spec_json"] = _record_text(workflow_id, agent_name, "vv/tb", "sva_spec.json", sva_spec_txt)
    artifacts["sva_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "SVA_README.md", bind_readme)

    primary_reset = resets[0]["name"] if resets else None
    primary_reset_active_low = bool(resets[0].get("active_low", False)) if resets else False
    primary_clock = clocks[0] if clocks else None

    report = {
        "type": "digital_sva_generation",
        "version": "1.2",
        "mode": mode,
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "primary_clock": primary_clock,
        "primary_reset": primary_reset,
        "primary_reset_active_low": primary_reset_active_low,
        "generated_dir": "vv/tb",
        "sva_module_name": module_name,
        "sva_bind_file": f"{module_name}_bind.sv",
        "assertion_output_signals": [p["name"] for p in sva_spec["ports"] if p["direction"] == "output"][:12],
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
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "digital_sva_assertions_agent.log", log_text)

    state.setdefault("vv", {})
    state["vv"]["sva"] = report
    state["vv"]["sva_spec"] = sva_spec

    state["sva_assertions_path"] = os.path.join(out_dir, f"{module_name}.sv")
    state["sva_spec_json"] = os.path.join(out_dir, "sva_spec.json")
    state["sva_bind_path"] = os.path.join(out_dir, f"{module_name}_bind.sv")

    _log(log_path, f"{agent_name} completed successfully.")
    return state
