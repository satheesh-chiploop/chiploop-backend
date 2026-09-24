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

from model_gateway import complete_text
from utils.artifact_utils import save_text_artifact_and_record

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


def _port_width_expr(port: Dict[str, Any]) -> str:
    width = port.get("width")
    msb = port.get("msb")
    lsb = port.get("lsb")
    rng = port.get("range")

    if isinstance(width, int) and width >= 1:
        return str(width)
    if isinstance(width, str) and width.strip():
        return width.strip()
    if msb is not None and lsb is not None:
        return f"(({msb}) - ({lsb}) + 1)"
    if isinstance(rng, str):
        m = re.match(r"\[\s*(.+?)\s*:\s*(.+?)\s*\]", rng.strip())
        if m:
            return f"(({m.group(1)}) - ({m.group(2)}) + 1)"
    return "1"


def _logic_decl(name: str, width_expr: Any) -> str:
    width = str(width_expr or "1").strip()
    try:
        width_i = int(width)
    except Exception:
        width_i = 1
    if width_i <= 1 and width in {"", "1"}:
        return f"input logic {name}"
    if width_i > 1:
        return f"input logic [{width_i - 1}:0] {name}"
    return f"input logic [(({width}) - 1):0] {name}"


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
                "width_expr": _port_width_expr(p),
            }
        )

    obligations = _behavioral_obligations(spec)
    # Assertions are bound to the module that owns each requirement.  A
    # top-only wrapper cannot legally observe child-local state/ports and would
    # turn hierarchical specs into permanent "missing checker" failures.
    from .digital_spec2rtl_conformance_agent import _structured_spec_modules
    module_specs = {
        str(module.get("name") or module.get("module_name") or "").strip(): module
        for module in _structured_spec_modules(spec)
        if str(module.get("name") or module.get("module_name") or "").strip()
    }
    targets: List[Dict[str, Any]] = []
    owner_names = list(dict.fromkeys(
        str(item.get("owner_module") or top) for item in obligations
    )) or [top]
    for owner in owner_names:
        module_spec = module_specs.get(owner) or (module_specs.get(top) if owner == top else {})
        owner_ports = [
            dict(port) for port in (module_spec.get("ports") or [])
            if isinstance(port, dict) and port.get("name")
        ]
        if owner == top and not owner_ports:
            owner_ports = [dict(port) for port in ports]
        owner_clocks, owner_resets = _infer_clocks_resets(module_spec or spec, owner_ports)
        targets.append({
            "module": owner,
            "assertion_module": f"{owner}_assertions",
            "clock_names": owner_clocks,
            "reset_signals": owner_resets,
            "ports": [{
                "name": str(port.get("name")),
                "direction": _normalize_direction(port.get("direction")),
                "width_expr": _port_width_expr(port),
            } for port in owner_ports],
            "behavioral_obligations": [
                item for item in obligations if str(item.get("owner_module") or top) == owner
            ],
        })
    return {
        "top_module": top,
        "soc_mode": soc_mode,
        "clock_names": clocks,
        "reset_signals": resets,
        "ports": port_points,
        "behavioral_obligations": obligations,
        "verification_targets": targets,
    }


def _behavioral_obligations(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create stable requirement/checker identities for behavioral verification."""
    # Reuse the exact Spec2RTL enumeration so REQ identifiers remain stable
    # from generation through assertion failure and RTL repair.
    from .digital_spec2rtl_conformance_agent import (
        _requirement_verification_method,
        _structured_requirements,
    )
    candidates = _structured_requirements(spec, "")

    obligations: List[Dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        owner = str(candidate.get("module") or "")
        section = str(candidate.get("section") or "")
        text = str(candidate.get("text") or "")
        verification_method = _requirement_verification_method(text, section)
        if verification_method in {"static_structural", "constraints_sta"}:
            continue
        requirement_id = f"REQ-{index:03d}"
        obligations.append({
            "requirement_id": requirement_id,
            "checker_id": f"a_{requirement_id.lower().replace('-', '_')}",
            "owner_module": owner or None,
            "section": section,
            "requirement": text,
            # This agent owns executable assertion collateral. Preserve the
            # classifier result for auditability, but do not tell the model an
            # obligation is "dynamic_simulation" and then reject it for not
            # producing an SVA checker.
            "verification_method": "systemverilog_assertion",
            "requirement_classification": verification_method,
            "status": "checker_generation_required",
        })
    return obligations


def _default_sva_module(module_name: str, sva_spec: Dict[str, Any]) -> str:
    clocks = list(sva_spec.get("clock_names", []))
    resets = list(sva_spec.get("reset_signals", []))
    ports = list(sva_spec.get("ports", []))

    primary_clock = clocks[0] if clocks else None
    primary_reset = resets[0]["name"] if resets else None
    primary_reset_active_low = bool(resets[0].get("active_low", False)) if resets else False

    inputs = [p["name"] for p in ports if p.get("direction") == "input"]
    outputs = [p["name"] for p in ports if p.get("direction") == "output"]
    widths = {str(p.get("name")): str(p.get("width_expr") or "1") for p in ports if p.get("name")}

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

    module_ports = [f"  {_logic_decl(nm, widths.get(nm, '1'))}" for nm in sorted(all_ports)]
    
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


def _default_sva_modules(sva_spec: Dict[str, Any]) -> str:
    targets = sva_spec.get("verification_targets") if isinstance(sva_spec.get("verification_targets"), list) else []
    if not targets:
        return _default_sva_module(f"{sva_spec.get('top_module')}_assertions", sva_spec)
    return "\n".join(
        _default_sva_module(str(target.get("assertion_module")), target)
        for target in targets if isinstance(target, dict)
    )


def _maybe_llm_expand(spec: Dict[str, Any], sva: str, log_path: str, sva_spec: Dict[str, Any], state: Dict[str, Any] | None = None) -> str:
    try:
        prompt = (
            "You are a senior RTL verification engineer.\n"
            "Expand this SVA scaffold conservatively.\n"
            "Constraints:\n"
            "- Use ONLY signal names present verbatim in SVA_SPEC or SPEC_JSON.\n"
            "- Do NOT invent any signal names, buses, protocols, or interfaces.\n"
            "- Keep the module name and port list intact.\n"
            "- Generate one labeled assertion for every behavioral_obligation.\n"
            "- Each label MUST exactly equal that obligation's checker_id.\n"
            "- Add a companion cover property named by replacing a_ with c_ in checker_id; cover the assertion antecedent/trigger so vacuity is measurable.\n"
            "- The c_req_* name is the cover statement LABEL. Internal property declarations must use a distinct name such as p_cover_req_*; property and block labels may not collide.\n"
            "- For behavior that occurs on the next edge/cycle, use non-overlapping implication |=> (or an equivalent explicit one-cycle delay).\n"
            "- Use overlapping implication |-> only for same-sample combinational relationships.\n"
            "- For synchronous reset, check the registered result after the reset edge; do not incorrectly require the pre-edge value to be reset.\n"
            "- 'Synchronous/registered state' does not mean the value is always stable; use $stable only when the requirement explicitly says hold, stable, or unchanged.\n"
            "- Never use tautologies such as signal == signal as requirement checkers.\n"
            "- Preserve requirement IDs in adjacent comments.\n"
            "- If an obligation cannot be expressed using available ports, do not invent signals; omit it so validation reports it missing.\n"
            "- Return SystemVerilog code only. No markdown.\n\n"
            f"SVA_SPEC:\n{json.dumps(sva_spec, indent=2)}\n\n"
            f"SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"SVA_CODE:\n{sva}\n"
        )
        out = complete_text(
            prompt,
            capability="verification_debug",
            agent_name="Digital Assertions (SVA) Agent",
            system="Return code only. No markdown.",
            state=state,
            temperature=0.1,
        ).strip()
        if out:
            out = re.sub(r"^```(?:systemverilog|sv|verilog)?\s*", "", out, flags=re.I)
            out = re.sub(r"\s*```\s*$", "", out)
            _log(log_path, "LLM expansion completed.")
            return out
    except Exception as e:
        _log(log_path, f"LLM expansion skipped/failed: {e}", level="warning")

    return sva


def _missing_behavioral_checker_ids(sva: str, sva_spec: Dict[str, Any]) -> List[str]:
    assertions = {
        item.lower() for item in re.findall(r"\b(a_req_\d+)\s*:\s*assert\s+property\b", sva, re.I)
    }
    covers = {
        item.lower() for item in re.findall(r"\b(c_req_\d+)\s*:\s*cover\s+property\b", sva, re.I)
    }
    missing: List[str] = []
    for obligation in sva_spec.get("behavioral_obligations") or []:
        if not isinstance(obligation, dict):
            continue
        checker = str(obligation.get("checker_id") or "").lower()
        cover = checker.replace("a_", "c_", 1)
        if checker not in assertions or cover not in covers:
            missing.append(str(obligation.get("requirement_id") or checker))
    return missing


def _checker_quality_issues(sva: str, sva_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Detect temporal sampling mistakes that commonly create false RTL failures."""
    named_properties = {
        name.lower(): body
        for name, body in re.findall(r"\bproperty\s+(\w+)\s*;(.*?)\bendproperty\b", sva, re.I | re.S)
    }
    assertion_bodies: Dict[str, str] = {}
    for checker, prop_name in re.findall(
        r"\b(a_req_\d+)\s*:\s*assert\s+property\s*\(\s*(\w+)\s*\)"
        r"(?=\s*(?:;|else\b))",
        sva,
        re.I,
    ):
        assertion_bodies[checker.lower()] = named_properties.get(prop_name.lower(), "")
    for checker, body in re.findall(
        r"\b(a_req_\d+)\s*:\s*assert\s+property\s*\((.*?)\)"
        r"(?=\s*(?:;|else\b))",
        sva,
        re.I | re.S,
    ):
        assertion_bodies.setdefault(checker.lower(), body)

    cover_bodies: Dict[str, str] = {}
    for cover, prop_name in re.findall(
        r"\b(c_req_\d+)\s*:\s*cover\s+property\s*\(\s*(\w+)\s*\)\s*;",
        sva,
        re.I,
    ):
        cover_bodies[cover.lower()] = named_properties.get(prop_name.lower(), "")
    for cover, body in re.findall(
        r"\b(c_req_\d+)\s*:\s*cover\s+property\s*\((.*?)\)\s*;",
        sva,
        re.I | re.S,
    ):
        cover_bodies.setdefault(cover.lower(), body)

    def constant_true(expression: str) -> bool:
        cleaned = re.sub(r"@\s*\([^)]*\)", " ", expression, flags=re.I)
        cleaned = re.sub(r"\bdisable\s+iff\s*\([^)]*\)", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"[()\s]", "", cleaned).lower()
        return cleaned in {"1", "1'b1", "1'd1", "true"}

    issues: List[Dict[str, str]] = []
    for obligation in sva_spec.get("behavioral_obligations") or []:
        if not isinstance(obligation, dict):
            continue
        checker = str(obligation.get("checker_id") or "").lower()
        requirement = str(obligation.get("requirement") or "")
        req_lower = requirement.lower()
        body = assertion_bodies.get(checker, "")
        if not body:
            continue
        cover_id = checker.replace("a_", "c_", 1)
        cover_body = cover_bodies.get(cover_id, "")
        if constant_true(body):
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": "assertion is constant true and cannot detect an RTL violation",
            })
        if re.search(r"\|[-=]>\s*(?:\(?\s*)?(?:1'b1|1'd1|true)(?:\s*\)?)?(?:\s|$)", body, re.I):
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": "assertion consequent is constant true and cannot check the required response",
            })
        self_comparison = re.search(
            r"\b([A-Za-z_][A-Za-z0-9_$]*)\b\s*(?:===|==|<=|>=)\s*\1\b",
            body,
            re.I,
        )
        if self_comparison:
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": f"assertion contains tautological self-comparison '{self_comparison.group(0)}'",
            })
        stable_signals = re.findall(r"\$stable\s*\(\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\)", body, re.I)
        antecedent = re.split(r"\|[-=]>", body, maxsplit=1)[0]
        explicit_disabled_hold = bool(
            re.search(r"\benable\s+gat", req_lower)
            and re.search(r"(?:!\s*\w*enable\w*|\b\w*enable\w*\s*==\s*(?:1'b0|0))", antecedent, re.I)
        )
        if stable_signals and not re.search(
            r"\b(?:hold|holds|held|stable|unchanged|retain|preserve)\b", req_lower
        ) and not explicit_disabled_hold:
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": (
                    "checker requires $stable(" + stable_signals[0]
                    + ") but the requirement does not specify hold/stability behavior"
                ),
            })
        if cover_body and constant_true(cover_body):
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": "non-vacuity cover is constant and does not measure requirement activation",
            })
        conditional_exception = re.search(
            r"\buntil\b.{0,120}?\b([A-Za-z_]\w*)\b\s*(<|>|<=|>=|==|!=)\s*"
            r"\b([A-Za-z_]\w*)\b.{0,40}?\bbecomes?\s+true\b",
            requirement,
            re.I | re.S,
        )
        if conditional_exception:
            lhs, comparator, rhs = conditional_exception.groups()
            if not re.search(
                rf"\b{re.escape(lhs)}\b\s*{re.escape(comparator)}\s*\b{re.escape(rhs)}\b",
                body,
                re.I,
            ):
                issues.append({
                    "requirement_id": str(obligation.get("requirement_id") or ""),
                    "checker_id": str(obligation.get("checker_id") or ""),
                    "issue": (
                        "checker drops the requirement's conditional exception "
                        f"{lhs} {comparator} {rhs}"
                    ),
                })
        sequential_transition = bool(re.search(
            r"\b(?:next\s+(?:rising\s+)?(?:edge|cycle)|holds?|advances?|increments?|wraps?|"
            r"synchronous(?:ly)?|on\s+(?:any\s+)?rising\s+edge)\b",
            req_lower,
        )) or bool(
            re.search(r"\breset\w*\b", req_lower)
            and re.search(r"\b(?:set|clear|zero|driven|forces?)\b", req_lower)
        )
        if sequential_transition and "|->" in body and "|=>" not in body and not re.search(r"##\s*1\b", body):
            issues.append({
                "requirement_id": str(obligation.get("requirement_id") or ""),
                "checker_id": str(obligation.get("checker_id") or ""),
                "issue": "sequential requirement uses same-sample |->; use |=> or explicit ##1",
            })
        relation = re.search(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s+"
            r"(?:evaluates?|is|equals?|shall\s+be)\b.{0,100}?\bcomparison\s+"
            r"([A-Za-z_][A-Za-z0-9_]*|\d+)\s*(<=|>=|==|!=|<|>)\s*"
            r"([A-Za-z_][A-Za-z0-9_]*|\d+)",
            requirement,
            re.I | re.S,
        )
        if relation:
            output_name, lhs, comparator, rhs = relation.groups()
            if not (
                re.search(rf"\b{re.escape(output_name)}\b", body, re.I)
                and re.search(rf"\b{re.escape(lhs)}\b\s*{re.escape(comparator)}\s*\b{re.escape(rhs)}\b", body, re.I)
            ):
                issues.append({
                    "requirement_id": str(obligation.get("requirement_id") or ""),
                    "checker_id": str(obligation.get("checker_id") or ""),
                    "issue": (
                        f"checker does not preserve explicit relation {output_name} from "
                        f"{lhs} {comparator} {rhs}"
                    ),
                })
    return issues


def _make_assertion_failures_terminal(sva: str) -> str:
    """Make requirement failures machine-readable without hanging the simulator.

    Verilator embedded through cocotb can report ``$fatal`` and finish the
    Python test while leaving the make/process wrapper alive.  Emit a labeled
    diagnostic instead; simulation execution treats that checker label as an
    authoritative failure even when the simulator exits zero.
    """
    requirement_action = re.compile(
        r"(?P<label>\b(a_req_\d+)\s*:\s*assert\s+property\s*\(\s*\w+\s*\))\s*"
        r"else\s+\$(?:fatal|error)\s*\([^;]*\)\s*;",
        re.I | re.S,
    )

    def report_requirement(match: re.Match) -> str:
        checker_match = re.search(r"\b(a_req_\d+)\b", match.group("label"), re.I)
        checker = checker_match.group(1) if checker_match else "a_req_unknown"
        return match.group("label") + f' else $display("ASSERTION_FAILURE {checker}");'

    sva = requirement_action.sub(report_requirement, sva)
    normalized = re.sub(r"\belse\s+\$error\s*\(", "else $fatal(1, ", sva, flags=re.I)
    bare = re.compile(
        r"(?P<label>\b[a-zA-Z_]\w*\s*:\s*assert\s+property\s*\(\s*[a-zA-Z_]\w*\s*\)\s*;)"
        r"(?!\s*else)",
        re.I,
    )

    def add_fatal(match: re.Match) -> str:
        label_match = re.match(r"\s*([a-zA-Z_]\w*)", match.group("label"))
        label = label_match.group(1) if label_match else "assertion"
        assertion = match.group("label").rstrip()
        if re.fullmatch(r"a_req_\d+", label, re.I):
            return assertion[:-1] + f' else $display("ASSERTION_FAILURE {label}");'
        return assertion[:-1] + f' else $fatal(1, "Assertion {label} failed.");'

    return bare.sub(add_fatal, normalized)


def _rename_property_label_collisions(sva: str) -> str:
    """Give properties and assertion/cover blocks distinct SV identifiers.

    Some simulators tolerate overlapping namespaces, while Verilator rejects a
    property and its labeled assertion/cover when both are named ``c_req_N``.
    Keep the externally tracked checker/cover label stable and rename only the
    internal property declaration and its references.
    """
    property_names = set(re.findall(r"\bproperty\s+([A-Za-z_]\w*)\s*;", sva, re.I))
    block_labels = set(re.findall(
        r"\b([A-Za-z_]\w*)\s*:\s*(?:assert|cover)\s+property\b", sva, re.I
    ))
    collisions = sorted(property_names.intersection(block_labels), key=len, reverse=True)
    normalized = sva
    occupied = {name.lower() for name in property_names | block_labels}
    for name in collisions:
        candidate = f"p_{name}"
        index = 2
        while candidate.lower() in occupied:
            candidate = f"p_{name}_{index}"
            index += 1
        occupied.add(candidate.lower())
        normalized = re.sub(
            rf"(\bproperty\s+){re.escape(name)}(\s*;)",
            rf"\g<1>{candidate}\g<2>",
            normalized,
            flags=re.I,
        )
        normalized = re.sub(
            rf"(\b(?:assert|cover)\s+property\s*\(\s*){re.escape(name)}(\s*\))",
            rf"\g<1>{candidate}\g<2>",
            normalized,
            flags=re.I,
        )
    return normalized


def _close_missing_checkers(
    spec: Dict[str, Any],
    sva: str,
    log_path: str,
    sva_spec: Dict[str, Any],
    state: Dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Retry only when deterministic checker inventory proves omissions."""
    max_attempts = max(1, min(int((state or {}).get("sva_checker_generation_max_attempts") or 3), 3))
    attempts = 1
    current = sva
    while attempts < max_attempts:
        missing = _missing_behavioral_checker_ids(current, sva_spec)
        quality_issues = _checker_quality_issues(current, sva_spec)
        affected = set(missing) | {item["requirement_id"] for item in quality_issues}
        if not affected:
            break
        obligations = [
            item for item in sva_spec.get("behavioral_obligations") or []
            if isinstance(item, dict) and item.get("requirement_id") in affected
        ]
        prompt = (
            "You are closing a deterministically detected SVA checker-completeness failure.\n"
            "Return the COMPLETE corrected SystemVerilog source, not a patch or explanation.\n"
            "Preserve every module declaration, port list, and existing assertion.\n"
            "Add exactly one assert property and one non-vacuity cover property for every missing obligation.\n"
            "Assertion labels must exactly match checker_id; cover labels replace a_ with c_.\n"
            "Keep internal property names distinct from assertion/cover labels (for example property p_cover_req_001 with label c_req_001).\n"
            "Use only ports declared in the owning verification target. Never invent signals.\n"
            "For next-edge/next-cycle behavior use |=> or an explicit one-cycle delay; reserve |-> for same-sample combinational behavior.\n"
            "Synchronous reset assertions must check state after the reset edge, not the pre-edge sampled state.\n"
            "Do not translate 'synchronous/registered state' into unconditional $stable; $stable is valid only for an explicit hold/stable/unchanged requirement.\n"
            "Never use signal == signal or another tautology as a checker.\n"
            f"MISSING_OBLIGATIONS:\n{json.dumps(obligations, indent=2)}\n\n"
            f"TEMPORAL_QUALITY_ISSUES:\n{json.dumps(quality_issues, indent=2)}\n\n"
            f"SVA_SPEC:\n{json.dumps(sva_spec, indent=2)}\n\n"
            f"SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"CURRENT_SVA:\n{current}\n"
        )
        try:
            candidate = complete_text(
                prompt,
                capability="verification_debug",
                agent_name="Digital Assertions (SVA) Agent",
                system="Return complete SystemVerilog code only. No markdown.",
                state=state,
                temperature=0.1,
            ).strip()
            attempts += 1
            candidate = re.sub(r"^```(?:systemverilog|sv|verilog)?\s*", "", candidate, flags=re.I)
            candidate = re.sub(r"\s*```\s*$", "", candidate)
            if candidate:
                current = candidate
            _log(
                log_path,
                f"SVA completeness attempt {attempts}/{max_attempts}; "
                f"missing={_missing_behavioral_checker_ids(current, sva_spec)}; "
                f"quality_issues={_checker_quality_issues(current, sva_spec)}",
            )
        except Exception as exc:
            attempts += 1
            _log(log_path, f"SVA completeness attempt {attempts}/{max_attempts} failed: {exc}", level="warning")
    return current, attempts


def _gen_bind_sv(top: str, module_name: str, sva_spec: Dict[str, Any]) -> str:
    targets = sva_spec.get("verification_targets") if isinstance(sva_spec.get("verification_targets"), list) else []
    if targets:
        blocks: List[str] = ["/* Auto-generated module-scoped SVA bindings. */"]
        for target in targets:
            if not isinstance(target, dict):
                continue
            owner = str(target.get("module") or "").strip()
            assertion_module = str(target.get("assertion_module") or "").strip()
            if not owner or not assertion_module:
                continue
            conns = [
                f"  .{port.get('name')}({port.get('name')})"
                for port in target.get("ports") or []
                if isinstance(port, dict) and port.get("name")
            ]
            blocks.append(
                f"bind {owner} {assertion_module} u_{assertion_module} (\n"
                + ",\n".join(conns)
                + "\n);"
            )
        return "\n\n".join(blocks) + "\n"
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


    sva_sv = _default_sva_modules(sva_spec)

    enable_llm_expand = str(os.getenv("CHIPLOOP_ENABLE_LLM_SVA_EXPAND", "1")).strip().lower() in ("1", "true", "yes")
    if enable_llm_expand:
        sva_sv = _maybe_llm_expand(spec, sva_sv, log_path, sva_spec, state=state)
        sva_sv, checker_generation_attempts = _close_missing_checkers(
            spec, sva_sv, log_path, sva_spec, state=state
        )
    else:
        _log(log_path, "LLM SVA expansion disabled; using deterministic scaffold.")
        checker_generation_attempts = 0

    sva_sv = _rename_property_label_collisions(sva_sv)
    sva_sv = _make_assertion_failures_terminal(sva_sv)

    bind_sv = _gen_bind_sv(top, module_name, sva_spec)

    checker_labels = set(re.findall(r"\b(a_req_\d+)\s*:\s*assert\s+property\b", sva_sv, re.I))
    cover_labels = set(re.findall(r"\b(c_req_\d+)\s*:\s*cover\s+property\b", sva_sv, re.I))
    for obligation in sva_spec.get("behavioral_obligations", []):
        checker_id = str(obligation.get("checker_id") or "")
        cover_id = checker_id.replace("a_", "c_", 1)
        assertion_present = checker_id.lower() in {item.lower() for item in checker_labels}
        cover_present = cover_id.lower() in {item.lower() for item in cover_labels}
        obligation["cover_id"] = cover_id
        obligation["assertion_generated"] = assertion_present
        obligation["nonvacuity_cover_generated"] = cover_present
        obligation["status"] = (
            "generated_pending_execution" if assertion_present and cover_present else "missing_checker"
        )
    missing_checker_ids = [
        item["requirement_id"] for item in sva_spec.get("behavioral_obligations", [])
        if item.get("status") == "missing_checker"
    ]
    checker_quality_issues = _checker_quality_issues(sva_sv, sva_spec)

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
        "sva_module_names": [
            target.get("assertion_module") for target in sva_spec.get("verification_targets", [])
            if isinstance(target, dict) and target.get("assertion_module")
        ],
        "sva_bind_file": f"{module_name}_bind.sv",
        "assertion_output_signals": [p["name"] for p in sva_spec["ports"] if p["direction"] == "output"][:12],
        "behavioral_obligation_count": len(sva_spec.get("behavioral_obligations", [])),
        "generated_behavioral_checker_count": len(sva_spec.get("behavioral_obligations", [])) - len(missing_checker_ids),
        "missing_behavioral_checker_ids": missing_checker_ids,
        "checker_generation_status": "pass" if not missing_checker_ids and not checker_quality_issues else "issues",
        "checker_generation_attempts": checker_generation_attempts,
        "checker_quality_issues": checker_quality_issues,
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

    if missing_checker_ids or checker_quality_issues:
        raise RuntimeError(
            "SVA checker generation remained incomplete or semantically invalid after "
            f"{checker_generation_attempts} attempt(s). Missing requirement checkers: "
            + ", ".join(missing_checker_ids)
            + "; temporal checker issues: "
            + ", ".join(item["requirement_id"] for item in checker_quality_issues)
            + ". See sva_generation_report.json for the exact obligations."
        )

    _log(log_path, f"{agent_name} completed successfully.")
    return state
