import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record
from .feature_contract_compiler import compile_feature_contracts

AGENT_NAME = "Digital Spec2RTL Conformance Agent"
RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}
GENERIC_PORT_WORDS = {
    "all", "and", "are", "bit", "bits", "clear", "cleared", "clock", "controlled", "cycle", "data",
    "every", "from", "high", "including", "interface", "is", "low", "map", "memory", "mapped", "nonzero",
    "output", "outputs", "read", "readback", "register", "registers", "reset", "return", "returns", "status",
    "the", "through", "to", "value", "when", "while", "write", "zero",
}
GENERIC_REQUIREMENT_WORDS = {
    "all", "and", "are", "assert", "based", "bit", "bits", "clear", "cleared", "clock", "configuration",
    "counter", "current", "cycle", "data", "decode", "decoded", "design", "edge", "every", "from", "generate",
    "high", "including", "input", "interface", "internal", "less", "live", "low", "mapped", "memory", "module",
    "must", "next", "nonzero", "operation", "output", "outputs", "programmed", "read", "readback", "register",
    "registers", "reset", "return", "returns", "shall", "should", "status", "than", "the", "through", "value",
    "when", "whenever", "while", "with", "write", "writes", "zero",
}


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _load_json_value(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip() and Path(value).exists():
        try:
            parsed = json.loads(_read_text(value))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _load_spec_json(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("spec_json", "digital_spec_json", "spec_json_path", "digital_spec_json_path"):
        parsed = _load_json_value(state.get(key))
        if parsed:
            return parsed
    workflow_dir = Path(str(state.get("workflow_dir") or ""))
    if workflow_dir.exists():
        for path in sorted((workflow_dir / "spec").glob("*_spec.json")):
            parsed = _load_json_value(str(path))
            if parsed:
                return parsed
    return None


def _load_regmap_json(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("regmap_json", "digital_regmap_json", "digital_regmap", "regmap_json_path"):
        parsed = _load_json_value(state.get(key))
        if parsed:
            return parsed
    workflow_dir = Path(str(state.get("workflow_dir") or ""))
    if workflow_dir.exists():
        for rel in ("digital/digital_regmap.json", "regmap/digital_regmap.json"):
            parsed = _load_json_value(str(workflow_dir / rel))
            if parsed:
                return parsed
    return None


def _strip_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _range_width(width: str) -> int:
    m = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", width or "")
    if not m:
        return 1
    return abs(int(m.group(1)) - int(m.group(2))) + 1


def _collect_rtl_files(state: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for key in ("rtl_files", "artifact_list"):
        value = state.get(key)
        if isinstance(value, list):
            files.extend(str(p) for p in value if isinstance(p, str) and Path(p).suffix.lower() in RTL_EXTENSIONS)
    explicit_paths = {str(Path(path).resolve()) for path in files if Path(path).exists()}

    # Search only roots that belong to this workflow. Scanning artifact_dir's
    # parent can ingest a same-named RTL file from another concurrent workflow
    # (or pytest worker) and produce false conformance failures.
    roots = [Path(str(state.get("artifact_dir") or ""))]
    workflow_dir = Path(str(state.get("workflow_dir") or ""))
    if str(state.get("workflow_dir") or "").strip():
        roots.append(workflow_dir)
    for root in roots:
        if root.exists():
            files.extend(str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower() in RTL_EXTENSIONS)

    seen: Dict[str, str] = {}
    for path in files:
        p = Path(path)
        if not p.exists():
            continue
        key = p.name
        current = seen.get(key)
        # Explicit inputs are inserted first and remain authoritative. Within
        # discovered collateral, a handoff copy may replace another discovered
        # copy, but never an explicit state-provided source.
        current_is_explicit = bool(current) and str(Path(current).resolve()) in explicit_paths
        if current is None or ("handoff" in p.parts and not current_is_explicit):
            seen[key] = str(p)
    return sorted(seen.values())


def _spec_text(state: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("spec_text", "digital_spec_text", "digital_spec", "spec", "requirements", "test_intent"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key in ("spec_json", "digital_spec_json"):
        value = state.get(key)
        if isinstance(value, dict):
            parts.append(json.dumps(value, indent=2))
        elif isinstance(value, str) and Path(value).exists():
            parts.append(_read_text(value))
    for key in ("spec_json_path", "digital_spec_json_path"):
        value = state.get(key)
        if isinstance(value, str) and Path(value).exists():
            parts.append(_read_text(value))
    return "\n\n".join(dict.fromkeys(parts))


def _top_spec_module(spec_obj: Optional[Dict[str, Any]], top_module: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(spec_obj, dict):
        return None
    hierarchy = spec_obj.get("hierarchy")
    if isinstance(hierarchy, dict):
        top = hierarchy.get("top_module")
        if isinstance(top, dict):
            return top
    if spec_obj.get("name") or spec_obj.get("ports"):
        return spec_obj
    return None


def _top_spec_module_name(spec_obj: Optional[Dict[str, Any]]) -> str:
    top = _top_spec_module(spec_obj)
    if isinstance(top, dict):
        return str(top.get("name") or "").strip()
    return ""


def _structured_spec_modules(spec_obj: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(spec_obj, dict):
        return []
    hierarchy = spec_obj.get("hierarchy")
    if isinstance(hierarchy, dict):
        mods = []
        if isinstance(hierarchy.get("top_module"), dict):
            mods.append(hierarchy["top_module"])
        mods.extend(m for m in hierarchy.get("modules", []) if isinstance(m, dict))
        return mods
    return [spec_obj] if isinstance(spec_obj.get("ports"), list) else []


def _expected_top_ports(spec_obj: Optional[Dict[str, Any]], spec: str) -> List[str]:
    top = _top_spec_module(spec_obj)
    if top and isinstance(top.get("ports"), list):
        return sorted({
            str(p.get("name")).strip()
            for p in top.get("ports", [])
            if isinstance(p, dict) and str(p.get("name") or "").strip()
        })
    return _extract_spec_ports(spec)


def _extract_modules(rtl_files: List[str]) -> List[Dict[str, Any]]:
    modules: List[Dict[str, Any]] = []
    mod_pat = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b(.*?)(?=\bendmodule\b)", re.DOTALL)
    ansi_port = re.compile(
        r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s*)?(\[[^\]]+\])?\s*([A-Za-z_][A-Za-z0-9_$]*)",
        re.IGNORECASE,
    )
    decl_port = re.compile(
        r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s*)?(\[[^\]]+\])?\s*([^;]+);",
        re.IGNORECASE,
    )
    seen = set()
    for path in rtl_files:
        text = _strip_comments(_read_text(path))
        for match in mod_pat.finditer(text):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)
            body = match.group(2)
            header = re.search(rf"\bmodule\s+{re.escape(name)}\b(.*?)\)\s*;", text, re.DOTALL)
            ports: List[Dict[str, Any]] = []
            port_seen = set()
            for source, regex in ((header.group(0) if header else "", ansi_port), (body, decl_port)):
                for pm in regex.finditer(source):
                    direction = pm.group(1).lower()
                    width = (pm.group(2) or "1").strip()
                    names = [pm.group(3)] if regex is ansi_port else pm.group(3).split(",")
                    for raw in names:
                        port_name = re.sub(r"=.*$", "", raw)
                        port_name = re.sub(r"\[[^\]]+\]", "", port_name).strip()
                        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", port_name or ""):
                            continue
                        if port_name in port_seen:
                            continue
                        port_seen.add(port_name)
                        ports.append({"name": port_name, "direction": direction, "width": width, "bits": _range_width(width)})
            modules.append({"name": name, "file": path, "ports": ports, "rtl_text": match.group(0) + "endmodule"})
    return modules


def _spec_tokens(spec: str) -> List[str]:
    stop = {
        "the", "and", "or", "for", "with", "when", "that", "this", "shall", "should", "must", "input", "output",
        "inputs", "outputs", "behavior", "generate", "systemverilog", "verilog", "register", "registers",
    }
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", spec or "")
    out = []
    for token in tokens:
        low = token.lower()
        if low not in stop and low not in out:
            out.append(low)
    return out[:200]


def _extract_requirements(spec: str) -> List[str]:
    reqs: List[str] = []
    for line in (spec or "").splitlines():
        clean = re.sub(r"^\s*[-*0-9.)]+\s*", "", line).strip()
        if len(clean) < 12:
            continue
        if re.search(r"\b(shall|must|should|when|if|reset|counter|fifo|interrupt|register|output|input|enable|error|clear|write|read)\b", clean, re.I):
            reqs.append(clean[:240])
    if not reqs:
        sentences = re.split(r"(?<=[.!?])\s+", spec or "")
        reqs = [s.strip()[:240] for s in sentences if len(s.strip()) >= 20][:24]
    return list(dict.fromkeys(reqs))[:50]


def _extract_spec_ports(spec: str) -> List[str]:
    ports = set()
    for match in re.finditer(r"\b(?:input|output|inout)s?\s*:?\s*([^.\n]+)", spec or "", re.I):
        segment = match.group(1)
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", segment):
            if name.lower() not in GENERIC_PORT_WORDS and name.lower() not in {"input", "output", "inout", "wire", "logic", "reg"}:
                ports.add(name)
    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]+\])", spec or ""):
        if name.lower() not in GENERIC_PORT_WORDS:
            ports.add(name)
    return sorted(ports)


def _edge_triggered_assignment_targets(rtl_text: str) -> set[str]:
    """Collect nonblocking-assignment targets from complete edge-triggered processes."""
    text = _strip_comments(rtl_text)
    starts = list(re.finditer(
        r"\balways(?:_ff)?\s*@\s*\([^)]*\b(?:posedge|negedge)\b[^)]*\)",
        text,
        re.I,
    ))
    targets: set[str] = set()
    for index, match in enumerate(starts):
        next_always = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        endmodule = text.find("endmodule", match.end(), next_always)
        end = endmodule if endmodule >= 0 else next_always
        body = text[match.end():end]
        targets.update(re.findall(
            r"(?:^|;|\bbegin\b|\bend\b|\belse\b|\))\s*([A-Za-z_][A-Za-z0-9_$]*)\s*<=",
            body,
            re.I,
        ))
    return targets


def _conditional_branch_body(rtl_text: str, condition_pattern: str) -> str:
    """Return the immediate branch body without crossing into a later else branch."""
    match = re.search(
        rf"\bif\s*\(\s*{condition_pattern}\s*\)\s*"
        r"(?:begin\b(?P<block>.*?)\bend|(?P<single>[^;]+;))",
        _strip_comments(rtl_text),
        re.I | re.S,
    )
    return (match.group("block") or match.group("single") or "") if match else ""


def _generic_behavior_evidence(requirement: str, rtl_text: str) -> List[str]:
    """Return structural evidence for common, application-independent RTL behavior."""
    req = requirement.lower()
    rtl = _strip_comments(rtl_text)
    evidence: List[str] = []
    valid_names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*valid[A-Za-z0-9_$]*)\b", rtl, re.I)
    has_valid_inhibit = any(re.search(
        rf"\b{re.escape(name)}\s*<=\s*(?:1'b0|\d+'[bdh]0+|0)\b", rtl, re.I
    ) for name in valid_names)

    if re.search(r"\b(?:decode|capture).*(?:packet|command)|(?:packet|command).*\bdecode", req):
        if re.search(r"\b(?:pkt|packet|cmd|command)[A-Za-z0-9_$]*\s*\[", rtl, re.I):
            evidence.append("bounded_packet_field_decode")
    if re.search(r"\b(?:ready|backpressure)\b", req):
        ready_assignments = re.findall(r"\b\w*ready\w*\s*(?:<=|=)\s*([^;]+)", rtl, re.I)
        # Reset-and-constant is not backpressure.  Require readiness to depend
        # on live capacity, occupancy, downstream readiness, or valid state.
        if any(re.search(r"\b\w*(?:full|empty|count|occup|space|fifo|queue|capacity|downstream)\w*\b", rhs, re.I)
               for rhs in ready_assignments):
            evidence.append("dynamic_ready_backpressure")
    if re.search(r"\b(?:validate|reject|malformed|stale|discontinuous|checksum|sequence)\b", req):
        predicates = re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_$]*(?:valid|malformed|stale|checksum|sequence|seq|fault|gap)[A-Za-z0-9_$]*\b",
            rtl, re.I,
        )
        if len(set(name.lower() for name in predicates)) >= 2 and re.search(r"\b(?:if|assign)\b", rtl, re.I):
            evidence.append("packet_validation_predicates")
    if "clamp" in req and re.search(r"\b(?:min|max)(?:imum)?\b", req):
        has_lower = bool(re.search(r"\b\w+\s*<\s*\w*(?:min|low)\w*", rtl, re.I))
        has_upper = bool(re.search(r"\b\w+\s*>\s*\w*(?:max|high)\w*", rtl, re.I))
        has_selected_value = bool(re.search(r"\b\w*(?:clamp|cmd|command)\w*\s*=\s*\w*(?:min|max|low|high)\w*", rtl, re.I))
        if has_lower and has_upper and has_selected_value:
            evidence.append("programmable_min_max_clamp")
    if "slew" in req:
        has_delta = bool(re.search(r"\b(?:diff|delta)\w*\s*=.*?-", rtl, re.I))
        has_limit = bool(re.search(r"\b(?:diff|delta)\w*\s*>\s*\w*(?:slew|limit|step)\w*", rtl, re.I))
        has_step = bool(re.search(r"\b\w*(?:cmd|command)\w*\s*=.*?[+-]\s*\w*(?:slew|limit|step)\w*", rtl, re.I))
        if has_delta and has_limit and has_step:
            evidence.append("bounded_slew_delta")
    if re.search(r"\b(?:deassert|inhibit|suppress).*(?:valid|validity)", req) and has_valid_inhibit:
        evidence.append("output_validity_inhibition")
    if re.search(r"\bno\s+(?:fallback|substitute|replacement)|must\s+not\s+(?:invent|create)", req):
        forbidden = re.search(r"\b(?:fallback|substitute|safe_cmd|safe_command)\w*\b", rtl, re.I)
        if not forbidden and has_valid_inhibit:
            evidence.append("no_fallback_value_and_validity_inhibited")
    if re.search(r"\b(?:status|telemetry)\b", req) and re.search(r"\b64[- ]bit\b", req):
        if re.search(r"\boutput\b(?:\s+(?:reg|wire|logic))?\s*\[\s*63\s*:\s*0\s*\]\s*\w*(?:status|telemetry)\w*", rtl, re.I):
            evidence.append("64bit_status_telemetry_output")
    if re.search(r"firmware-visible.*(?:csr|mmio)|(?:csr|mmio).*control plane", req):
        required_roles = ("addr", "wdata", "rdata", "valid", "write", "ready")
        if all(re.search(rf"\b\w*(?:csr|mmio)\w*{role}\w*\b|\b\w*{role}\w*(?:csr|mmio)\w*\b", rtl, re.I) for role in required_roles):
            evidence.append("firmware_visible_csr_mmio_interface")
    if re.search(r"packet format.*packet type|packet type.*format", req):
        format_ids = set(re.findall(r"\b\w*format(?:_version)?\w*\b", rtl, re.I))
        type_ids = set(re.findall(r"\b\w*(?:packet|pkt)_type\w*\b", rtl, re.I))
        compared = lambda name: bool(re.search(rf"\b{re.escape(name)}\b\s*(?:==|!=)", rtl, re.I))
        if any(compared(name) for name in format_ids) and any(compared(name) for name in type_ids):
            evidence.append("distinct_packet_format_and_type_checks")
    if re.search(r"semantic (?:fields|outputs).*(?:controller|explicit ports)|explicit ports.*semantic", req):
        semantic_outputs = re.findall(r"\boutput\b[^;]*\b(?:cfg_|\w+_cfg(?:_out)?|\w+_out)\w*", rtl, re.I)
        if len(semantic_outputs) >= 2:
            evidence.append("explicit_semantic_output_ports")
    if re.search(r"velocity envelope.*packet validity|packet validity.*velocity envelope", req):
        has_range = bool(re.search(r"\b\w*velocity\w*\s*>=.*&&.*\b\w*velocity\w*\s*<=", rtl, re.I))
        has_valid = bool(re.search(r"\b\w*(?:pkt|packet)\w*ok\w*\s*=", rtl, re.I))
        if has_range and has_valid:
            evidence.append("velocity_envelope_and_packet_validity")
    if re.search(r"\b(?:status|telemetry).*(?:fault|interrupt)|fault.*interrupt", req):
        has_status = bool(re.search(r"\b(?:status|telemetry)\w*\s*(?:<=|=)", rtl, re.I))
        has_fault = bool(re.search(r"\b(?:fault|irq|interrupt)\w*\s*(?:<=|=)", rtl, re.I))
        if has_status and has_fault:
            evidence.append("status_telemetry_and_fault_outputs")
    if re.search(r"\b(?:rising|positive)\s+edge\b.*\bclk\b|\bposedge\s+clk\b", req):
        controls = re.findall(r"\balways(?:_ff)?\s*@\s*\(([^)]*)\)", rtl, re.I)
        if controls and all(re.search(r"\bposedge\s+clk\b", control, re.I) for control in controls if re.search(r"\b(?:pos|neg)edge\b", control, re.I)):
            evidence.append("sequential_updates_on_posedge_clk")
    if re.search(r"\b(?:readable|readback|write transactions?|address decode|uniquely address|reachable).*\b(?:register|field|address|writable)", req):
        has_decode = bool(re.search(r"\bcase\s*\(\s*\w*(?:addr|address)\w*\s*\)", rtl, re.I))
        has_read = bool(re.search(r"\b\w*(?:rdata|read_data)\w*\s*<=", rtl, re.I))
        has_write = bool(re.search(r"\b\w*(?:wdata|write_data)\w*\s*\[", rtl, re.I))
        if has_decode and (has_read or has_write):
            evidence.append("register_decode_and_access_paths")
    if re.search(r"configuration semantics.*explicit outputs|explicit.*configuration.*outputs", req):
        if re.search(r"\boutput\b[^;]*\bcfg_[A-Za-z0-9_$]+", rtl, re.I) and re.search(r"\bcfg_[A-Za-z0-9_$]+\s*<=", rtl, re.I):
            evidence.append("explicit_configuration_outputs")
    if re.search(r"\b(?:latch|sticky).*(?:status|fault)|(?:status|fault).*\b(?:latch|sticky)", req):
        if re.search(r"\b\w*(?:fault|status|sticky)\w*\s*<=\s*1'b1", rtl, re.I):
            evidence.append("latched_status_fault_state")
    return list(dict.fromkeys(evidence))


def _match_score(
    requirement: str,
    rtl_text: str,
    rtl_names: Iterable[str],
    structural_context: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    req_lower = requirement.lower()
    rtl_without_comments = _strip_comments(rtl_text)
    structural_context = structural_context or {}
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", requirement)
        if w.lower() not in GENERIC_REQUIREMENT_WORDS
    ]
    names = {n.lower() for n in rtl_names}
    unique_words = list(dict.fromkeys(words))
    evidence = [w for w in unique_words if w in names or re.search(rf"\b{re.escape(w)}\b", rtl_text, re.I)]
    evidence.extend(_generic_behavior_evidence(requirement, rtl_without_comments))
    if structural_context.get("register_contract_complete") is True and re.search(
        r"every declared register.*reachable|hidden register bank|inaccessible internal state|software-visible register.*(?:unreachable|reachable)",
        req_lower,
    ):
        evidence.append("complete_register_contract_traceability")
    if re.search(r"\beach register.*uniquely.*address decode", req_lower):
        case_blocks = re.findall(
            r"\bcase\s*\(\s*\w*(?:addr|address)\w*\s*\)(.*?)\bendcase\b",
            rtl_without_comments,
            re.I | re.S,
        )
        labels = [re.findall(r"\b\d+'h[0-9a-f]+\s*:", block, re.I) for block in case_blocks]
        if labels and all(len(items) == len(set(item.lower() for item in items)) for items in labels):
            evidence.append("unique_register_address_case_items")
    if re.search(r"top-level outputs?.*real rtl drivers|may not be tied only to child inputs", req_lower):
        driven = True
        for output_name in structural_context.get("output_ports") or []:
            direct = re.search(
                rf"\b(?:assign\s+)?{re.escape(str(output_name))}\s*(?:<=|=)",
                rtl_without_comments,
                re.I,
            )
            child_connection = any(
                re.search(rf"\boutput\b[^;]*\b{re.escape(formal)}\b", rtl_without_comments, re.I)
                for formal in re.findall(
                    rf"\.([A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*{re.escape(str(output_name))}\s*\)",
                    rtl_without_comments,
                    re.I,
                )
            )
            if not direct and not child_connection:
                driven = False
                break
        if driven and structural_context.get("output_ports"):
            evidence.append("all_top_outputs_have_rtl_drivers")
    if re.search(r"register block.*not generate actuator commands", req_lower):
        register_blocks = re.findall(
            r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*(?:csr|reg|mmio)[A-Za-z0-9_$]*\b(.*?)\bendmodule\b",
            rtl_without_comments,
            re.I | re.S,
        )
        if register_blocks and all(not re.search(r"\bactuator_(?:cmd|command)", block, re.I) for block in register_blocks):
            evidence.append("register_block_has_no_actuator_command_path")
    if re.search(r"resets all control registers.*documented defaults", req_lower):
        reset_body = _conditional_branch_body(rtl_without_comments, r"!\s*(?:rst_n|reset_n)")
        has_control_resets = len(re.findall(r"\b(?:cfg_|\w*(?:ctrl|control)\w*)\w*\s*<=", reset_body, re.I)) >= 2
        has_response_reset = bool(re.search(r"\b\w*(?:ready|rvalid|read\w*valid)\w*\s*<=\s*(?:1'b0|0)", reset_body, re.I))
        if has_control_resets and has_response_reset and structural_context.get("register_contract_complete") is True:
            evidence.append("control_and_read_state_reset_with_complete_decode")
    instance_types = [
        match.group(1).lower()
        for match in re.finditer(
            r"(?:^|[;])\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\([^;]*?\)\s*)?"
            r"[A-Za-z_][A-Za-z0-9_$]*\s*\(",
            rtl_without_comments,
            re.I | re.M | re.S,
        )
        if match.group(1).lower() not in {
            "module", "always", "always_ff", "always_comb", "begin", "end", "if", "else", "for", "while", "case", "assign",
        }
    ]
    # Negative structural requirements are commonly emitted as coordinated
    # lists (for example, "no memories, buses, or submodules").  Recognize the
    # individual nouns instead of depending on one exact sentence template.
    structure_noun = r"(?:memor(?:y|ies)|ram|rom|storage\s+arrays?|bus(?:es)?|interconnects?|submodules?|child\s+modules?|hierarchy|component\s+instances?)"
    has_negative_structure_clause = bool(
        re.search(rf"\b(?:no|without)\b[^.\n]*\b{structure_noun}\b", req_lower)
        or re.search(rf"\bfree\s+of\b[^.\n]*\b{structure_noun}\b", req_lower)
        or re.search(rf"\bneither\b[^.\n]*\b{structure_noun}\b", req_lower)
        or re.search(r"\b(?:does|do|shall|must)\s+not\s+(?:contain|include|instantiate|use)\b", req_lower)
    )
    no_hierarchy_required = bool(
        "no internal hierarchy" in req_lower
        or re.search(r"\bdoes\s+not\s+contain\b.*\bhierarchical\s+submodules?\b", req_lower)
        or (has_negative_structure_clause and re.search(
            r"\b(?:submodules?|hierarchy|hierarchical|child\s+modules?|component\s+instances?)\b", req_lower
        ))
    )
    if no_hierarchy_required and len(re.findall(r"\bmodule\b", rtl_without_comments, re.I)) == 1 and not instance_types:
        evidence.append("no_internal_hierarchy")
    no_memory_required = bool(
        re.search(r"\bno\s+(?:internal\s+)?memory\s+macros?\b", req_lower)
        or re.search(r"\bdoes\s+not\s+contain\b.*\bmemory\s+macros?\b", req_lower)
        or (has_negative_structure_clause and re.search(
            r"\b(?:memor(?:y|ies)|ram|rom|storage\s+arrays?)\b", req_lower
        ))
    )
    inferred_memories = re.findall(
        r"\b(?:reg|logic|bit|integer|wire)\b\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?"
        r"[A-Za-z_][A-Za-z0-9_$]*\s*\[[^\]]+\]\s*;",
        rtl_without_comments,
        re.I,
    )
    if no_memory_required and not inferred_memories and not any(
        re.search(r"(?:sram|ram|rom|memory|mem_macro)", kind, re.I) for kind in instance_types
    ):
        evidence.append("no_memories")
    no_bus_required = bool(
        has_negative_structure_clause and re.search(r"\b(?:bus(?:es)?|interconnects?)\b", req_lower)
    )
    rtl_identifiers = {
        item.lower() for item in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", rtl_without_comments)
    }
    protocol_bus_markers = re.compile(
        r"(?:^|_)(?:axi\d*|axil|apb|ahb|wishbone|avalon|tilelink|ace|chi|ocp|plb)(?:_|$)|"
        r"(?:^|_)(?:awvalid|awready|awaddr|arvalid|arready|araddr|wvalid|wready|wdata|wstrb|"
        r"bvalid|bready|bresp|rvalid|rready|rdata|rresp|paddr|psel|penable|pwrite|pwdata|"
        r"prdata|pready|pslverr|haddr|htrans|hwrite|hwdata|hrdata|hready|hresp|cyc_i|stb_i)(?:_|$)",
        re.I,
    )
    if no_bus_required and not any(protocol_bus_markers.search(item) for item in rtl_identifiers):
        evidence.append("no_bus_interfaces")
    architectural_minimality_required = bool(
        re.search(r"\b(?:avoid|no|without|free\s+of)\b[^.\n]*(?:hidden\s+state|extra\s+handshakes?|undeclared\s+interfaces?)", req_lower)
    )
    minimality_expectations = []
    if architectural_minimality_required:
        hidden_state_prohibited = bool(re.search(r"\bhidden\s+state\b", req_lower))
        memory_prohibited = bool(re.search(
            r"\b(?:no|without|avoid|free\s+of)\b[^.\n]*\b(?:memor(?:y|ies)|ram|rom|storage\s+arrays?)\b",
            req_lower,
        ))
        extra_interface_prohibited = bool(re.search(
            r"\b(?:extra\s+handshakes?|undeclared\s+interfaces?)\b", req_lower
        ))
        sequential_targets = _edge_triggered_assignment_targets(rtl_without_comments)
        output_names = {str(item) for item in structural_context.get("output_ports") or []}
        observable_state = set()
        for target in sequential_targets:
            if target in output_names or any(re.search(
                rf"\bassign\s+{re.escape(output_name)}\s*=\s*[^;]*\b{re.escape(target)}\b",
                rtl_without_comments,
                re.I,
            ) for output_name in output_names):
                observable_state.add(target)
        if hidden_state_prohibited:
            if not sequential_targets or observable_state == sequential_targets:
                evidence.append("no_hidden_sequential_state")
            minimality_expectations.append("no_hidden_sequential_state")
        if memory_prohibited:
            if not inferred_memories and not any(
                re.search(r"(?:sram|ram|rom|memory|mem_macro)", kind, re.I) for kind in instance_types
            ):
                evidence.append("no_memories")
            minimality_expectations.append("no_memories")
        if extra_interface_prohibited:
            if structural_context.get("interface_exact") is True:
                evidence.append("no_extra_interface_handshakes")
            minimality_expectations.append("no_extra_interface_handshakes")
    synchronous_clock_match = re.search(
        r"\b(?:fully\s+)?synchronous(?:ly)?\s+to\s+([A-Za-z_][A-Za-z0-9_$]*)\b",
        requirement,
        re.I,
    )
    no_clock_gating_required = bool(
        re.search(r"\bno\s+(?:internal\s+)?clock\s+gating\b", req_lower)
        or re.search(r"\bno\s+gated\s+clocks?\b", req_lower)
        or re.search(r"\b(?:must|shall|does|do)\s+not\s+gate\s+(?:the\s+)?clocks?\b", req_lower)
    )
    synchronous_clock = synchronous_clock_match.group(1) if synchronous_clock_match else None
    sequential_event_controls = re.findall(
        r"\balways(?:_ff)?\s*@\s*\(([^)]*\b(?:posedge|negedge)\b[^)]*)\)",
        rtl_without_comments,
        re.I,
    )
    sequential_clock_signals = []
    for event_control in sequential_event_controls:
        sequential_clock_signals.extend(
            signal for _, signal in re.findall(
                r"\b(posedge|negedge)\s+([A-Za-z_][A-Za-z0-9_$]*)\b", event_control, re.I
            )
        )
    if synchronous_clock and sequential_clock_signals and all(
        signal.lower() == synchronous_clock.lower() for signal in sequential_clock_signals
    ):
        evidence.append(f"fully_synchronous_to_{synchronous_clock}")
    derived_clock_assignment = bool(re.search(
        rf"\b(?:assign\s+|wire\b[^;=]*\b)(?:[A-Za-z_][A-Za-z0-9_$]*(?:clk|clock)|(?:clk|clock)[A-Za-z0-9_$]*)\s*=\s*"
        rf"[^;]*\b{re.escape(synchronous_clock or 'clk')}\b[^;]*(?:&|\||\?|\b(?:and|or)\b)",
        rtl_without_comments,
        re.I,
    ))
    if no_clock_gating_required and not derived_clock_assignment and (
        not synchronous_clock
        or not sequential_clock_signals
        or all(signal.lower() == synchronous_clock.lower() for signal in sequential_clock_signals)
    ):
        evidence.append("no_internal_clock_gating")
    if re.search(r"\bsynthesiz", req_lower) and not re.search(
        r"(^|[^A-Za-z_])(initial|force|release|fork|join)\b|#[ \t]*\d+",
        _strip_comments(rtl_text),
        re.I,
    ):
        evidence.append("synthesizable_rtl_subset")
    if re.search(r"\b(?:must\s+not|no)\b.*\blatch", req_lower):
        combinational_blocks = re.findall(
            r"\balways(?:_comb)?\s*(?:@\s*\(\s*\*\s*\))?\s*begin\b(.*?)\bend\b",
            rtl_text,
            re.I | re.S,
        )
        if not re.search(r"\balways\s*@\s*\(\s*\*\s*\)|\balways_comb\b", rtl_text, re.I):
            evidence.append("no_combinational_latch_sites")
        elif combinational_blocks and all("else" in block.lower() or "default:" in block.lower() for block in combinational_blocks):
            evidence.append("complete_combinational_assignment_structure")
    arithmetic_width = re.search(r"\bunsigned\b.*?\b(\d+)\s*-?bit\b", req_lower)
    if arithmetic_width:
        width = int(arithmetic_width.group(1))
        declared_widths = [
            abs(int(msb) - int(lsb)) + 1
            for msb, lsb in re.findall(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", rtl_text)
        ]
        if "signed" not in rtl_text.lower() and declared_widths and all(item == width for item in declared_widths):
            evidence.append(f"unsigned_{width}bit_arithmetic")
    for name in sorted(names):
        if "_" not in name or len(name) < 5:
            continue
        parts = [p for p in name.split("_") if p and p not in GENERIC_REQUIREMENT_WORDS]
        if parts and all(re.search(rf"\b{re.escape(part)}\b", req_lower) for part in parts):
            evidence.append(name)
    if re.search(r"\bread", req_lower) and {"rd_en", "rd_addr", "rd_data"} & names:
        evidence.append("read_path")
    if re.search(r"\bread|readback", req_lower) and "rd_data" in names:
        evidence.append("rd_data")
    if re.search(r"\bwrite", req_lower) and {"wr_en", "wr_addr", "wr_data"} & names:
        evidence.append("write_path")
    if "decode" in req_lower and "register" in req_lower and {"wr_addr", "rd_addr"} & names:
        expected_regs = [
            "control",
            "status",
            "threshold",
            "latest_temp",
            "sample_count",
            "irq_status",
            "irq_clear",
        ]
        reg_hits = [reg for reg in expected_regs if reg in req_lower and reg in rtl_text.lower()]
        addr_hits = re.findall(r"\b\d+'h[0-9a-fA-F]+\b", rtl_text)
        has_case_decode = bool(re.search(r"\bcase\s*\(\s*(?:rd_addr|wr_addr)\s*\)", rtl_text, re.I))
        has_explicit_addr_decode = bool(re.search(r"\b(?:rd_addr|wr_addr)\s*==\s*\d+'h[0-9a-fA-F]+", rtl_text, re.I))
        if (has_case_decode or has_explicit_addr_decode) and (len(reg_hits) >= 3 or len(set(addr_hits)) >= 4):
            evidence.append("register_address_decode")
    if "counter" in req_lower and ("counter_value" in names or re.search(r"\bcounter_value", rtl_text, re.I)):
        evidence.append("counter_value")
    if "reset" in req_lower and re.search(r"\breset_n\b", rtl_text, re.I) and re.search(r"<=\s*(?:\d+'h00|\d+'d0|1'b0|0)\b", rtl_text, re.I):
        evidence.append("reset_zero")
    all_state_reset_required = bool(
        re.search(r"\bsynchronous(?:ly)?\b", req_lower)
        and re.search(r"\breset\b", req_lower)
        and re.search(r"\ball\s+(?:registers?|sequential\s+state|state)\b", req_lower)
    )
    reset_contract_signal = re.search(r"\b(reset_n|rst_n|reset|rst)\b", requirement, re.I)
    if all_state_reset_required and reset_contract_signal:
        reset_name = reset_contract_signal.group(1)
        reset_is_active_low = reset_name.lower().endswith("_n") or bool(re.search(r"active[- ]low", req_lower))
        if reset_name.lower() not in rtl_identifiers:
            preferred_reset_names = (
                ("reset_n", "rst_n", "por_n") if reset_is_active_low
                else ("reset", "rst")
            )
            reset_name = next(
                (candidate for candidate in preferred_reset_names if candidate in rtl_identifiers),
                reset_name,
            )
        reset_assertion = rf"!\s*{re.escape(reset_name)}" if reset_is_active_low else re.escape(reset_name)
        sequential_targets = _edge_triggered_assignment_targets(rtl_without_comments)
        proven_targets = set()
        zero_literal = r"(?:\d+'[bdh]0+|1'b0|0)\b"
        reset_branch_body = _conditional_branch_body(rtl_without_comments, reset_assertion)
        for target in sequential_targets:
            direct_reset = re.search(
                rf"\b{re.escape(target)}\s*<=\s*{zero_literal}", reset_branch_body, re.I
            )
            next_state_assignments = re.findall(
                rf"\b{re.escape(target)}\s*<=\s*([A-Za-z_][A-Za-z0-9_$]*)\s*;",
                rtl_without_comments,
                re.I,
            )
            next_state_reset = any(re.search(
                rf"\bassign\s+{re.escape(next_name)}\s*=\s*\(\s*{reset_assertion}\s*\)\s*\?\s*{zero_literal}",
                rtl_without_comments,
                re.I,
            ) for next_name in next_state_assignments)
            if direct_reset or next_state_reset:
                proven_targets.add(target)
        if sequential_targets and proven_targets == sequential_targets:
            evidence.append("all_sequential_state_synchronously_reset_zero")
    # Prove reset requirements per named output. A reset assignment to some
    # unrelated state register must not satisfy prose such as "pwm_out is
    # driven low". This catches combinational outputs that remain active while
    # reset is asserted.
    reset_low_outputs = list(dict.fromkeys(
        group
        for match in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_$]*)\b\s+(?:is|shall\s+be|must\s+be)\s+"
            r"(?:driven|forced|set)\s+(?:to\s+)?(?:low|zero|0)\b|"
            r"\b([A-Za-z_][A-Za-z0-9_$]*)\b\s+(?:is|shall\s+be|must\s+be)\s+"
            r"(?:cleared|deasserted)\b",
            requirement,
            re.I,
        )
        for group in match.groups()
        if group
    ))
    declared_outputs = {str(name).lower() for name in structural_context.get("output_ports") or []}
    if declared_outputs:
        reset_low_outputs = [name for name in reset_low_outputs if name.lower() in declared_outputs]
    reset_signal_match = re.search(r"\b(reset_n|rst_n|reset|rst)\b", requirement, re.I)
    reset_signal = reset_signal_match.group(1) if reset_signal_match else None
    unproven_reset_low_outputs = []
    if reset_signal:
        active_low_reset = reset_signal.lower().endswith("_n") or bool(
            re.search(r"\bactive[- ]low\b", req_lower)
        )
        asserted_condition = rf"!\s*{re.escape(reset_signal)}" if active_low_reset else re.escape(reset_signal)
        for output_name in reset_low_outputs:
            output_pattern = re.escape(output_name)
            sequential_reset_assignment = bool(re.search(
                rf"if\s*\(\s*{asserted_condition}\s*\)\s*(?:begin\b.*?)?\b{output_pattern}\s*<=\s*"
                rf"(?:\d+'[bdh]0+|1'b0|0)\b",
                rtl_without_comments,
                re.I | re.S,
            ))
            continuous_assignment = re.search(
                rf"\bassign\s+{output_pattern}\s*=\s*(?P<expr>[^;]+);",
                rtl_without_comments,
                re.I,
            )
            combinational_reset_gate = bool(
                continuous_assignment
                and re.search(rf"\b{re.escape(reset_signal)}\b", continuous_assignment.group("expr"), re.I)
            )
            if sequential_reset_assignment or combinational_reset_gate:
                evidence.append(f"reset_low_{output_name}")
            else:
                unproven_reset_low_outputs.append(output_name)
    if re.search(r"\bincrement", req_lower) and re.search(r"\+\s*(?:\d+'[bdh])?0*1\b|\+\s*1'b1\b", rtl_text, re.I):
        evidence.append("increment_logic")
    if re.search(r"\b(?:periodic|repeating|cyclic)\b", req_lower) and re.search(r"\bcount", req_lower):
        enable_conditions = re.findall(
            r"(?:else\s+)?if\s*\(\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\)",
            rtl_without_comments,
            re.I,
        )
        has_enable_control = any(
            signal.lower() == "enable"
            or signal.lower().endswith("_en")
            or "enable" in signal.lower()
            for signal in enable_conditions
        )
        incremented_states = list(dict.fromkeys(
            match.group(1) or match.group(2)
            for match in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*<=\s*\1\s*\+\s*(?:\d+'[bdh])?0*1\b|"
                r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*<=\s*\2\s*\+\s*1'b1\b",
                rtl_without_comments,
                re.I,
            )
        ))
        for state_name in incremented_states if has_enable_control else []:
            terminal_compares = re.findall(
                rf"\b{re.escape(state_name)}\s*(?:==|>=)\s*([A-Za-z_][A-Za-z0-9_$]*)\b",
                rtl_without_comments,
                re.I,
            )
            has_terminal_compare = any(
                re.search(r"(?:period|limit|terminal|reload|modulus)", signal, re.I)
                for signal in terminal_compares
            )
            has_wrap_to_zero = bool(re.search(
                rf"\b{re.escape(state_name)}\s*<=\s*(?:\d+'[bdh]0+|0)\b",
                rtl_without_comments,
                re.I,
            ))
            if has_terminal_compare and has_wrap_to_zero:
                evidence.append("enabled_periodic_count_sequence")
                break
    if re.search(r"\bwrap", req_lower) and re.search(r">=|==", rtl_text) and re.search(r"<=\s*(?:\d+'h00|\d+'d0|0)\b", rtl_text, re.I):
        evidence.append("wrap_logic")
    if (
        ("rollover" in req_lower or "reload" in req_lower or ("period" in req_lower and "counter" in req_lower))
        and re.search(r"\b[A-Za-z_][A-Za-z0-9_$]*\s*(?:==|>=)\s*[A-Za-z0-9_$]*period[A-Za-z0-9_$]*\b", rtl_text, re.I)
        and re.search(r"\b[A-Za-z0-9_$]*(?:count|counter)[A-Za-z0-9_$]*\s*<=\s*(?:\d+'h0+|\d+'d0|\d+'b0|0)\b", rtl_text, re.I)
    ):
        evidence.append("period_rollover_logic")
    if "unmapped" in req_lower and re.search(r"\bdefault\s*:", rtl_text, re.I) and re.search(r"default\s*:\s*[A-Za-z_][A-Za-z0-9_$]*\s*<=\s*(?:\d+'h00|\d+'d0|0)", rtl_text, re.I):
        evidence.append("default_zero")

    if (
        ("ownership" in req_lower or "architectural" in req_lower or "contract" in req_lower)
        and ("internal" in req_lower or "state" in req_lower or "register" in req_lower or "sticky" in req_lower)
    ):
        declared_state = re.findall(
            r"\b(?:reg|logic)\b\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*;",
            rtl_text,
            re.I,
        )
        state_like = [
            name for name in declared_state
            if re.search(r"(state|status|count|counter|control|ctrl|irq|valid|busy|temp|threshold|sticky|flag|enable)", name, re.I)
        ]
        has_behavior = bool(re.search(r"\balways\s*@\s*\(", rtl_text, re.I))
        if len(set(state_like)) >= 3 and has_behavior:
            evidence.append("internal_state_contract")

    # Recognize common register-bit implementation idioms. This is still
    # evidence-based: require concrete RTL assignments, not only names.
    if "control" in req_lower and "enable" in req_lower:
        if (
            re.search(r"\bcontrol_\w*\s*\[\s*0\s*\]\s*<=\s*wr_data\s*\[\s*0\s*\]", rtl_text, re.I)
            or re.search(r"\benable_(?:reg|out)\s*<=\s*wr_data\s*\[\s*0\s*\]", rtl_text, re.I)
        ):
            evidence.append("CONTROL.ENABLE stored")
    if "control" in req_lower and ("irq_enable" in req_lower or ("irq" in req_lower and "enable" in req_lower)):
        if (
            re.search(r"\bcontrol_\w*\s*\[\s*2\s*\]\s*<=\s*wr_data\s*\[\s*2\s*\]", rtl_text, re.I)
            or re.search(r"\bcontrol_irq_enable\w*\s*<=\s*wr_data\s*\[\s*2\s*\]", rtl_text, re.I)
            or re.search(r"\birq_enable_(?:reg|out)\s*<=\s*wr_data\s*\[\s*2\s*\]", rtl_text, re.I)
        ):
            evidence.append("CONTROL.IRQ_ENABLE stored")
    if "sample count" in req_lower and ("completed" in req_lower or "track" in req_lower or "increment" in req_lower):
        if re.search(r"\bsample_count\w*\s*<=\s*sample_count\w*\s*\+\s*(?:\d+'[bdh])?0*1\b", rtl_text, re.I):
            evidence.append("sample_count_increment")
    if "adc_valid_seen" in req_lower or ("adc" in req_lower and "valid" in req_lower and "seen" in req_lower):
        if re.search(r"\bstatus_adc_valid_seen\w*\s*<=\s*1'b1", rtl_text, re.I):
            evidence.append("sticky_adc_valid_seen")
    if "irq_status" in req_lower and "bit 1" in req_lower and "sample_done" in req_lower and re.search(r"\blatch", req_lower):
        scalar_sample_done_set = bool(re.search(r"\birq_status_sample_done\s*<=\s*1'b1", rtl_text, re.I))
        scalar_sample_done_clear = bool(re.search(r"\birq_status_sample_done\s*<=\s*1'b0", rtl_text, re.I))
        if (
            scalar_sample_done_set
            and re.search(r"\birq_status_\w*\s*\[\s*1\s*\]\s*<=\s*irq_status_sample_done", rtl_text, re.I)
        ):
            evidence.append("IRQ_STATUS.sample_done latch")
        if scalar_sample_done_set and scalar_sample_done_clear:
            evidence.append("IRQ_STATUS.sample_done scalar latch")
        if (
            re.search(r"\bstatus_\w*\s*\[\s*0\s*\]\s*<=\s*1'b1", rtl_text, re.I)
            and re.search(r"\birq_status_\w*\s*\[\s*1\s*\]\s*<=\s*1'b1", rtl_text, re.I)
        ):
            evidence.append("STATUS/IRQ_STATUS.sample_done latch")
    if "status.sample_done" in req_lower and "irq_status.sample_done" in req_lower and re.search(r"\blatch", req_lower):
        scalar_status_set = bool(re.search(r"\bstatus_sample_done\s*<=\s*1'b1", rtl_text, re.I))
        scalar_status_clear = bool(re.search(r"\bstatus_sample_done\s*<=\s*1'b0", rtl_text, re.I))
        scalar_irq_set = bool(re.search(r"\birq_status_sample_done\s*<=\s*1'b1", rtl_text, re.I))
        scalar_irq_clear = bool(re.search(r"\birq_status_sample_done\s*<=\s*1'b0", rtl_text, re.I))
        if (
            re.search(r"\bstatus_\w*\s*\[\s*0\s*\]\s*<=\s*1'b1", rtl_text, re.I)
            and re.search(r"\birq_status_\w*\s*\[\s*1\s*\]\s*<=\s*1'b1", rtl_text, re.I)
        ):
            evidence.append("STATUS/IRQ_STATUS.sample_done latch")
        if scalar_status_set and scalar_status_clear and scalar_irq_set and scalar_irq_clear:
            evidence.append("STATUS/IRQ_STATUS.sample_done scalar latch")
    if "sticky" in req_lower and "status" in req_lower and ("irq_status" in req_lower or "irq" in req_lower):
        status_sticky = bool(re.search(r"\bstatus_(?:sample_done|alert_pending|adc_valid_seen)\w*\s*<=\s*1'b1", rtl_text, re.I))
        irq_sticky = bool(re.search(r"\birq_status_(?:sample_done|alert)\w*\s*<=\s*1'b1", rtl_text, re.I))
        independent_clear = bool(
            re.search(r"\bwr_data\s*\[\s*0\s*\]", rtl_text, re.I)
            and re.search(r"\bwr_data\s*\[\s*1\s*\]", rtl_text, re.I)
            and re.search(r"\bstatus_(?:sample_done|alert_pending)\w*\s*<=\s*1'b0", rtl_text, re.I)
            and re.search(r"\birq_status_(?:sample_done|alert)\w*\s*<=\s*1'b0", rtl_text, re.I)
        )
        if status_sticky and irq_sticky and independent_clear:
            evidence.append("sticky STATUS/IRQ_STATUS independent clears")
    if "sticky" in req_lower and "status" in req_lower and "interrupt" in req_lower:
        status_sticky = bool(re.search(r"\bstatus_(?:sample_done|alert_pending|adc_valid_seen)\w*\s*<=\s*1'b1", rtl_text, re.I))
        irq_or_interrupt_sticky = bool(
            re.search(r"\birq_status_(?:sample_done|alert)\w*\s*<=\s*1'b1", rtl_text, re.I)
            or re.search(r"\balert_status\w*\s*<=\s*1'b1", rtl_text, re.I)
        )
        if status_sticky and irq_or_interrupt_sticky:
            evidence.append("sticky status/interrupt indicators")
    if ("clear semantics" in req_lower or ("specified" in req_lower and "clear" in req_lower)) and "sticky" in req_lower:
        clear_alert = bool(
            re.search(r"\b(?:control_alert_clear|alert_clear|irq_clear_alert)\b", rtl_text, re.I)
            and re.search(r"\b(?:status_alert_pending|irq_status_alert|alert_status\w*)\s*<=\s*1'b0", rtl_text, re.I)
        )
        clear_sample = bool(
            re.search(r"\b(?:irq_clear_sample_done)\b", rtl_text, re.I)
            and re.search(r"\b(?:status_sample_done|irq_status_sample_done)\s*<=\s*1'b0", rtl_text, re.I)
        )
        set_sticky = bool(
            re.search(r"\b(?:status_sample_done|status_alert_pending|irq_status_sample_done|irq_status_alert)\s*<=\s*1'b1", rtl_text, re.I)
        )
        if clear_alert and clear_sample and set_sticky:
            evidence.append("sticky clear semantics")
    if "irq_clear" in req_lower and "bit 1" in req_lower and "sample_done" in req_lower and re.search(r"\bclear", req_lower):
        has_irq_clear_bit1_decode = bool(
            re.search(r"\bwr_addr\s*==\s*\d+'h0*18", rtl_text, re.I)
            and re.search(r"\bwr_data\s*\[\s*1\s*\]", rtl_text, re.I)
        )
        has_irq_clear_bit1_signal = bool(
            re.search(r"\birq_clear\w*sample_done\b", rtl_text, re.I)
            or re.search(r"\birq_clear\w*\s*\[\s*1\s*\]", rtl_text, re.I)
        )
        clears_scalar_sample_done = bool(
            re.search(r"\birq_status_sample_done\w*\s*<=\s*1'b0", rtl_text, re.I)
            and re.search(r"\bstatus_sample_done\w*\s*<=\s*1'b0", rtl_text, re.I)
        )
        clears_vector_sample_done = bool(
            re.search(r"\birq_status_\w*\s*\[\s*1\s*\]\s*<=\s*1'b0", rtl_text, re.I)
            and re.search(r"\bstatus_\w*\s*\[\s*0\s*\]\s*<=\s*1'b0", rtl_text, re.I)
        )
        if (has_irq_clear_bit1_decode or has_irq_clear_bit1_signal) and clears_scalar_sample_done:
            evidence.append("IRQ_CLEAR.sample_done clear")
        if (has_irq_clear_bit1_decode or has_irq_clear_bit1_signal) and clears_vector_sample_done:
            evidence.append("IRQ_CLEAR.sample_done clear")
    if "first sample" in req_lower and ("history" in req_lower or "observed sample" in req_lower):
        if re.search(r"\bprev_sample_valid\b", rtl_text, re.I) and re.search(r"\?\s*\([^;]+prev_sample[^;]+:\s*adc_code", rtl_text, re.I):
            evidence.append("first_sample_history_init")
        elif re.search(r"\bprev_sample_valid\b", rtl_text, re.I) and re.search(r"else\s+begin\s+[^;]*temp_code\s*<=\s*adc_code", rtl_text, re.I | re.DOTALL):
            evidence.append("first_sample_history_init")
    if "periodic sample request" in req_lower or ("enable is set" in req_lower and "sample_start" in req_lower):
        if re.search(r"\bcontrol_\w*\s*\[\s*0\s*\]", rtl_text, re.I) and re.search(r"\bsample_req\s*<=\s*1'b1", rtl_text, re.I):
            evidence.append("enable_periodic_sample_req")
        if re.search(r"\bwr_data\s*\[\s*1\s*\]", rtl_text, re.I) and re.search(r"\bsample_req\s*<=\s*1'b1", rtl_text, re.I):
            evidence.append("sample_start_priority_path")
    if "only clear" in req_lower and "clear" in req_lower:
        protected_regs = [
            "latest_temp",
            "latest_temp_reg",
            "threshold_code",
            "threshold_code_reg",
            "sample_count",
            "sample_count_reg",
        ]
        clear_signal_names = [
            "control_alert_clear",
            "alert_clear",
            "irq_clear_alert",
            "irq_clear_sample_done",
        ]
        clear_blocks = []
        for signal in clear_signal_names:
            for m in re.finditer(rf"\bif\s*\(\s*{re.escape(signal)}\s*\)\s*begin(?P<body>.*?)end", rtl_text, re.I | re.DOTALL):
                clear_blocks.append(m.group("body"))
        if clear_blocks:
            touched_protected = any(
                re.search(rf"\b{re.escape(reg)}\s*<=", body, re.I)
                for body in clear_blocks
                for reg in protected_regs
            )
            clears_status_only = any(
                re.search(r"\b(?:alert_status\w*|status_alert_pending|status_sample_done|irq_status_alert|irq_status_sample_done)\s*<=\s*1'b0", body, re.I)
                for body in clear_blocks
            )
            if clears_status_only and not touched_protected:
                evidence.append("clear side effects limited to specified status bits")
    if "expose" in req_lower and "temp" in req_lower and "threshold" in req_lower:
        if re.search(r"\boutput\b\s*(?:\[[^\]]+\]\s*)?temp_code\b", rtl_text, re.I) and re.search(
            r"\boutput\b\s*(?:\[[^\]]+\]\s*)?threshold_code\b", rtl_text, re.I
        ):
            evidence.append("dedicated temp_code/threshold_code outputs")

    addresses = re.findall(r"0x([0-9a-fA-F]+)", requirement)
    for addr in addresses:
        addr_int = int(addr, 16)
        addr_patterns = [
            rf"\b\d+'h0*{addr_int:x}\b",
            rf"\b\d+'h0*{addr_int:X}\b",
            rf"\b0x0*{addr_int:x}\b",
            rf"\b0x0*{addr_int:X}\b",
        ]
        if any(re.search(pat, rtl_text, re.I) for pat in addr_patterns):
            evidence.append(f"0x{addr.upper()}")
    evidence = list(dict.fromkeys(evidence))
    if not unique_words and not addresses:
        return "inconclusive", []
    semantic_hits = {
        "CONTROL.ENABLE stored",
        "CONTROL.IRQ_ENABLE stored",
        "IRQ_STATUS.sample_done latch",
        "STATUS/IRQ_STATUS.sample_done latch",
        "IRQ_CLEAR.sample_done clear",
        "first_sample_history_init",
        "enable_periodic_sample_req",
        "sample_start_priority_path",
        "sample_count_increment",
        "sticky_adc_valid_seen",
        "internal_state_contract",
        "register_address_decode",
        "sticky STATUS/IRQ_STATUS independent clears",
        "sticky clear semantics",
        "sticky status/interrupt indicators",
        "clear side effects limited to specified status bits",
        "dedicated temp_code/threshold_code outputs",
        "period_rollover_logic",
        "enabled_periodic_count_sequence",
        "bounded_packet_field_decode",
        "packet_validation_predicates",
        "programmable_min_max_clamp",
        "bounded_slew_delta",
        "output_validity_inhibition",
        "no_fallback_value_and_validity_inhibited",
        "status_telemetry_and_fault_outputs",
        "sequential_updates_on_posedge_clk",
        "register_decode_and_access_paths",
        "explicit_configuration_outputs",
        "latched_status_fault_state",
        "dynamic_ready_backpressure",
        "64bit_status_telemetry_output",
        "firmware_visible_csr_mmio_interface",
        "distinct_packet_format_and_type_checks",
        "explicit_semantic_output_ports",
        "velocity_envelope_and_packet_validity",
        "complete_register_contract_traceability",
        "unique_register_address_case_items",
        "all_top_outputs_have_rtl_drivers",
        "register_block_has_no_actuator_command_path",
        "control_and_read_state_reset_with_complete_decode",
        "synthesizable_rtl_subset",
        "no_combinational_latch_sites",
        "complete_combinational_assignment_structure",
    }
    if (
        re.search(r"\bsynchronous(?:ly)?\b", req_lower)
        and re.search(r"\breset", req_lower)
        and re.search(
            r"\balways(?:_ff)?\s*@\s*\([^)]*\bor\s+(?:pos|neg)edge\s+(?:reset|reset_n|rst|rst_n)\b",
            rtl_text,
            re.I,
        )
    ):
        return "missing", ["asynchronous_reset_sensitivity_conflicts_with_synchronous_requirement"]
    if unproven_reset_low_outputs:
        return "missing", [
            f"reset_low_not_implemented:{name}" for name in unproven_reset_low_outputs[:8]
        ]
    if all_state_reset_required and "all_sequential_state_synchronously_reset_zero" not in evidence:
        return "missing", ["not_all_sequential_state_has_synchronous_zero_reset"]
    if (
        "pwm_out" in req_lower
        and ("combinational" in req_lower or "level-based" in req_lower)
        and not (
            re.search(r"\bassign\s+pwm_out\s*=\s*[^;]*(?:counter\w*\s*<\s*duty_cycle|duty_cycle\s*>\s*counter\w*)", rtl_text, re.I)
            or re.search(r"\balways(?:_comb)?\s*@?\s*\(\s*\*\s*\).*?\bpwm_out\s*=", rtl_text, re.I | re.S)
            or any(re.search(
                rf"\balways(?:_comb)?\s*@?\s*\(\s*\*\s*\).*?\b{re.escape(alias)}\s*=\s*"
                rf"[^;]*(?:counter\w*\s*<\s*duty_cycle|duty_cycle\s*>\s*counter\w*)",
                rtl_text,
                re.I | re.S,
            ) for alias in re.findall(r"\bassign\s+pwm_out\s*=\s*([A-Za-z_][A-Za-z0-9_$]*)\s*;", rtl_text, re.I))
        )
    ):
        return "missing", ["pwm_out_combinational_compare_not_implemented"]
    negative_structure_expectations = []
    if no_hierarchy_required:
        negative_structure_expectations.append("no_internal_hierarchy")
    if no_memory_required:
        negative_structure_expectations.append("no_memories")
    if no_bus_required:
        negative_structure_expectations.append("no_bus_interfaces")
    if negative_structure_expectations:
        return (
            ("matched", evidence[:8])
            if all(item in evidence for item in negative_structure_expectations)
            else ("missing", evidence[:8])
        )
    if minimality_expectations:
        return (
            ("matched", evidence[:8])
            if all(item in evidence for item in minimality_expectations)
            else ("missing", evidence[:8])
        )
    clock_structure_expectations = []
    if synchronous_clock:
        clock_structure_expectations.append(f"fully_synchronous_to_{synchronous_clock}")
    if no_clock_gating_required:
        clock_structure_expectations.append("no_internal_clock_gating")
    if clock_structure_expectations:
        return (
            ("matched", evidence[:8])
            if all(item in evidence for item in clock_structure_expectations)
            else ("missing", evidence[:8])
        )
    if arithmetic_width:
        arithmetic_evidence = f"unsigned_{int(arithmetic_width.group(1))}bit_arithmetic"
        return ("matched", evidence[:8]) if arithmetic_evidence in evidence else ("missing", evidence[:8])
    if semantic_hits.intersection(evidence):
        return "matched", evidence[:8]
    if addresses and not any(item.startswith("0x") for item in evidence):
        return ("partial", evidence[:8]) if evidence else ("missing", [])
    if addresses and any(item.startswith("0x") for item in evidence) and len(evidence) >= 2:
        return "matched", evidence[:8]
    if len(evidence) >= 2 or (len(evidence) == 1 and (len(unique_words) <= 2 or "_" in evidence[0])):
        return "matched", evidence[:8]
    if evidence:
        return "partial", evidence[:8]
    return "missing", []


def _register_evidence(spec: str, rtl_text: str, state: Dict[str, Any], spec_obj: Optional[Dict[str, Any]], regmap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    fields: List[str] = []
    registers: List[Tuple[str, Optional[str]]] = []

    def collect_registers(container: Any) -> None:
        if not isinstance(container, dict):
            return
        raw_regs = container.get("registers")
        if not isinstance(raw_regs, list):
            raw_regs = ((container.get("regmap") or {}).get("registers") if isinstance(container.get("regmap"), dict) else [])
        for reg in raw_regs or []:
            if not isinstance(reg, dict):
                continue
            reg_name = str(reg.get("name") or "").strip()
            address = str(reg.get("offset") or reg.get("address") or "").strip() or None
            if reg_name:
                registers.append((reg_name, address))
            for field in reg.get("fields") or []:
                if isinstance(field, dict) and str(field.get("name") or "").strip():
                    fields.append(str(field["name"]).strip())

    if isinstance(regmap, dict):
        collect_registers(regmap.get("regmap") if isinstance(regmap.get("regmap"), dict) else regmap)
    if isinstance(spec_obj, dict):
        collect_registers(spec_obj.get("register_contract") or {})
    fields.extend(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:_reg|_cfg|_ctrl|_status))\b", spec or "", re.I))
    unique = sorted(dict.fromkeys(f for f in fields if f.lower() not in {"reserved"}))
    rtl_identifiers = {
        name.lower()
        for name in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", rtl_text or "")
    }

    def field_matched(field: str) -> bool:
        low = field.lower()
        if re.search(rf"\b{re.escape(field)}\b", rtl_text, re.I):
            return True
        if re.search(rf"\b{re.escape(low)}_(?:r|reg|q|d)\b", rtl_text, re.I):
            return True
        tokens = [t for t in re.split(r"[^a-zA-Z0-9]+", low) if t and t not in {"bit", "field"}]
        if tokens and any(all(tok in ident for tok in tokens) for ident in rtl_identifiers):
            return True
        # Descriptive suffixes such as ``_word`` and ``_value`` do not define
        # hardware identity.  A concrete RTL symbol carrying every remaining
        # semantic token is valid evidence (telemetry_word -> telemetry_shadow).
        semantic_tokens = [t for t in tokens if t not in {"word", "value", "data"}]
        if semantic_tokens and any(all(tok in ident for tok in semantic_tokens) for ident in rtl_identifiers):
            return True
        return False

    matched = [
        f for f in unique
        if field_matched(f)
    ]
    addresses = []
    raw_reg = json.dumps(regmap or spec_obj or {})
    for value in re.findall(r'"(?:offset|address)"\s*:\s*"(0x[0-9a-fA-F]+)"', raw_reg):
        addresses.append(value)
    matched_addresses = []
    for value in sorted(dict.fromkeys(addresses)):
        addr_int = int(value, 16)
        if re.search(rf"\b\d+'h0*{addr_int:x}\b", rtl_text, re.I) or re.search(rf"\b{re.escape(value)}\b", rtl_text, re.I):
            matched_addresses.append(value)
    # The normalized spec and generated regmap legitimately describe the same
    # register. One source may omit its address while the other supplies it;
    # merge those records by name and prefer concrete evidence before sorting.
    # Sorting raw (name, address) tuples can compare None with str and crash a
    # successful RTL run during its reporting/signoff step.
    registers_by_name: Dict[str, Optional[str]] = {}
    for reg_name, address in registers:
        current = registers_by_name.get(reg_name)
        if reg_name not in registers_by_name or (not current and address):
            registers_by_name[reg_name] = address
    canonical_registers = sorted(registers_by_name.items(), key=lambda item: item[0].lower())

    matched_registers = []
    missing_registers = []
    for reg_name, address in canonical_registers:
        address_matched = bool(address and address in matched_addresses)
        name_matched = bool(re.search(rf"\b{re.escape(reg_name)}\b", rtl_text, re.I) or re.search(rf"\b{re.escape(reg_name.lower())}\b", rtl_text, re.I))
        if address_matched or name_matched:
            matched_registers.append(reg_name)
        else:
            missing_registers.append(reg_name)
    missing_fields = [f for f in unique if f not in matched]
    missing_addresses = [a for a in sorted(dict.fromkeys(addresses)) if a not in matched_addresses]
    return {
        "expected": unique,
        "matched": matched,
        "expected_registers": [r[0] for r in canonical_registers],
        "matched_registers": matched_registers,
        "expected_addresses": sorted(dict.fromkeys(addresses)),
        "matched_addresses": matched_addresses,
        "missing": missing_fields,
        "missing_registers": missing_registers,
        "missing_addresses": missing_addresses,
        "status": "pass" if (unique or addresses or registers) and not missing_fields and not missing_addresses and not missing_registers else ("not_applicable" if not unique and not addresses and not registers else "issues"),
    }


def _structured_requirements(spec_obj: Optional[Dict[str, Any]], spec: str) -> List[Dict[str, str]]:
    reqs: List[Dict[str, str]] = []
    for mod in _structured_spec_modules(spec_obj):
        module_name = str(mod.get("name") or mod.get("module_name") or "").strip()
        for key in ("responsibilities", "behavior_rules", "must_drive", "must_receive"):
            values = mod.get(key)
            if isinstance(values, list):
                reqs.extend(
                    {"module": module_name, "section": key, "text": str(v).strip()[:240]}
                    for v in values if len(str(v).strip()) >= 8
                )
        for key in ("reset_behavior",):
            if isinstance(mod.get(key), str) and mod[key].strip():
                reqs.append({"module": module_name, "section": key, "text": mod[key].strip()[:240]})
    if reqs:
        unique: List[Dict[str, str]] = []
        seen = set()
        for item in reqs:
            identity = (item["module"], item["section"], item["text"])
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        return unique
    return [{"module": "", "section": "free_text", "text": text} for text in _extract_requirements(spec)]


def _feature_contract_evidence(
    spec_obj: Optional[Dict[str, Any]], modules: List[Dict[str, Any]], top_module: str
) -> Dict[str, Any]:
    """Statically bind extracted feature stimulus/checkers to the RTL interface.

    This proves structural executability only; cycle-accurate behavior remains
    the responsibility of the independent Verification workflow.
    """
    top_spec = _top_spec_module(spec_obj, top_module) or {}
    spec_ports = top_spec.get("ports") if isinstance(top_spec.get("ports"), list) else []
    contracts = compile_feature_contracts(spec_obj or {}, spec_ports)
    rtl_top = next((module for module in modules if module.get("name") == top_module), None)
    if rtl_top is None and len(modules) == 1:
        rtl_top = modules[0]
    rtl_ports = {
        str(port.get("name") or ""): str(port.get("direction") or "").lower()
        for port in ((rtl_top or {}).get("ports") or []) if isinstance(port, dict)
    }
    results = []
    for contract in contracts:
        stimulus_names = sorted({
            str(name)
            for step in contract.get("stimulus_steps") or []
            for name in (step.get("signals") or {}).keys()
        })
        expected_names = sorted(str(name) for name in (contract.get("expected") or {}).keys())
        missing_stimulus = [name for name in stimulus_names if name not in rtl_ports]
        missing_expected = [name for name in expected_names if name not in rtl_ports]
        wrong_stimulus_direction = [
            name for name in stimulus_names
            if name in rtl_ports and rtl_ports[name] not in {"input", "inout"}
        ]
        wrong_expected_direction = [
            name for name in expected_names
            if name in rtl_ports and rtl_ports[name] not in {"output", "inout"}
        ]
        passed = bool(contract.get("executable")) and not any((
            missing_stimulus, missing_expected,
            wrong_stimulus_direction, wrong_expected_direction,
            contract.get("unresolved_bindings") or [],
        ))
        results.append({
            "feature_id": contract.get("feature_id"),
            "status": "pass" if passed else "issues",
            "stimulus_signals": stimulus_names,
            "expected_signals": expected_names,
            "missing_stimulus_signals": missing_stimulus,
            "missing_expected_signals": missing_expected,
            "wrong_stimulus_directions": wrong_stimulus_direction,
            "wrong_expected_directions": wrong_expected_direction,
            "unresolved_bindings": contract.get("unresolved_bindings") or [],
        })
    if not contracts:
        return {"status": "not_applicable", "checked": 0, "passed": 0, "failed": 0, "features": []}
    failed = sum(1 for item in results if item["status"] != "pass")
    return {
        "status": "pass" if failed == 0 else "issues",
        "checked": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "features": results,
        "scope": "static interface binding; behavioral results are verified separately",
    }


def _add_check(counts: Dict[str, int], status: str) -> None:
    if status == "not_applicable":
        return
    bucket = {
        "pass": "matched",
        "matched": "matched",
        "partial": "partial",
        "issues": "missing",
        "missing": "missing",
        "inconclusive": "inconclusive",
        "setup_issue": "inconclusive",
    }.get(status, "inconclusive")
    counts["checked"] += 1
    counts[bucket] += 1


def _clock_reset_evidence(spec: str, modules: List[Dict[str, Any]]) -> Dict[str, Any]:
    ports = [p["name"] for m in modules for p in m.get("ports", [])]
    clock_ports = [p for p in ports if re.search(r"(^|_)(clk|clock)($|_)", p, re.I)]
    reset_ports = [p for p in ports if re.search(r"(^|_)(rst|reset|reset_n|rst_n)($|_)", p, re.I)]
    spec_mentions_reset = bool(re.search(r"\b(reset|reset_n|rst_n|rst)\b", spec or "", re.I))
    return {
        "clock_ports": sorted(dict.fromkeys(clock_ports)),
        "reset_ports": sorted(dict.fromkeys(reset_ports)),
        "status": "pass" if clock_ports and (reset_ports or not spec_mentions_reset) else "issues",
    }


def _overall_status(summary: Dict[str, int], setup_issues: List[str]) -> str:
    if setup_issues:
        return "setup_issue"
    checked = summary.get("checked", 0)
    if checked == 0:
        return "inconclusive"
    if summary.get("missing", 0) == 0 and summary.get("partial", 0) == 0:
        return "pass"
    if summary.get("matched", 0) >= max(1, checked // 2):
        return "partial"
    return "issues"


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    artifact_dir = Path(str(state.get("artifact_dir") or "."))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    spec_obj = _load_spec_json(state)
    regmap_obj = _load_regmap_json(state)
    spec = _spec_text(state)
    rtl_files = _collect_rtl_files(state)
    modules = _extract_modules(rtl_files)
    rtl_text = "\n".join(_strip_comments(_read_text(path)) for path in rtl_files)
    rtl_names = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", rtl_text))

    setup_issues: List[str] = []
    if not spec.strip():
        setup_issues.append("missing_spec")
    if not rtl_files:
        setup_issues.append("missing_rtl")
    if not modules:
        setup_issues.append("no_parseable_modules")

    top_module = str(state.get("top_module") or _top_spec_module_name(spec_obj) or "").strip()
    module_names = [m["name"] for m in modules]
    top_status = "pass" if top_module and top_module in module_names else ("not_applicable" if not top_module else "issues")

    spec_ports = _expected_top_ports(spec_obj, spec)
    top_module_ports = {
        p["name"]
        for m in modules
        if not top_module or m["name"] == top_module
        for p in m.get("ports", [])
    }
    rtl_ports = {p["name"] for m in modules for p in m.get("ports", [])}
    comparison_ports = top_module_ports or rtl_ports
    matched_ports = [p for p in spec_ports if p in comparison_ports]
    missing_ports = [p for p in spec_ports if p not in comparison_ports]
    extra_ports = [p for p in comparison_ports if p not in spec_ports]
    interface_status = "pass" if spec_ports and not missing_ports and not extra_ports else ("inconclusive" if not spec_ports else "issues")
    register_check = _register_evidence(spec, rtl_text, state, spec_obj, regmap_obj)
    top_spec = _top_spec_module(spec_obj, top_module) or {}
    output_ports = {
        str(port.get("name") or "")
        for port in (top_spec.get("ports") or [])
        if isinstance(port, dict) and str(port.get("direction") or "").lower() in {"output", "out", "o", "inout", "io"}
    }
    structural_context = {
        "output_ports": output_ports,
        "interface_exact": bool(spec_ports) and not missing_ports and not extra_ports,
        "register_contract_complete": register_check.get("status") == "pass",
    }

    requirements = _structured_requirements(spec_obj, spec)
    requirement_results = []
    counts = {"checked": 0, "matched": 0, "partial": 0, "missing": 0, "inconclusive": 0}
    if setup_issues:
        for _ in requirements:
            _add_check(counts, "inconclusive")
    else:
        module_rtl = {str(module.get("name") or ""): str(module.get("rtl_text") or "") for module in modules}
        for idx, obligation in enumerate(requirements, start=1):
            requirement = obligation["text"]
            owner = obligation.get("module") or ""
            if owner and owner not in module_rtl:
                status, evidence = "missing", [f"owner_module_not_found:{owner}"]
            else:
                scoped_rtl = module_rtl[owner] if owner else rtl_text
                scoped_names = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", scoped_rtl))
                status, evidence = _match_score(requirement, scoped_rtl, scoped_names, structural_context)
            _add_check(counts, status)
            requirement_results.append({
                "id": f"REQ-{idx:03d}",
                "module": owner or None,
                "section": obligation.get("section"),
                "requirement": requirement,
                "status": status,
                "evidence_tokens": evidence,
            })

    clock_reset_check = _clock_reset_evidence(spec, modules)
    feature_contract_check = _feature_contract_evidence(spec_obj, modules, top_module)
    _add_check(counts, top_status)
    _add_check(counts, interface_status)
    _add_check(counts, register_check["status"])
    _add_check(counts, clock_reset_check["status"])
    _add_check(counts, feature_contract_check["status"])

    status = _overall_status(counts, setup_issues)
    report = {
        "agent": AGENT_NAME,
        "status": status,
        "summary": counts,
        "setup_issues": setup_issues,
        "top_module": {"expected": top_module or None, "modules_found": module_names, "status": top_status},
        "interface": {
            "status": interface_status,
            "expected_ports": spec_ports,
            "matched_ports": matched_ports,
            "missing_ports": missing_ports,
            "extra_ports": extra_ports,
            "rtl_ports": sorted(rtl_ports),
        },
        "register_map": register_check,
        "clock_reset": clock_reset_check,
        "feature_contracts": feature_contract_check,
        "requirements": requirement_results,
        "rtl_files": rtl_files,
        "modules": modules,
        "notes": [
            "This is a conformance analysis, not a formal proof.",
            "Missing or partial items should be reviewed and turned into executable assertions/tests where needed.",
            "Inconclusive is preserved when the checker lacks enough evidence; no fake pass is reported.",
        ],
    }

    md_lines = [
        "# Spec-to-RTL Conformance Report",
        "",
        f"Status: **{status}**",
        "",
        f"- Requirements checked: {counts.get('checked', 0)}",
        f"- Matched: {counts.get('matched', 0)}",
        f"- Partial: {counts.get('partial', 0)}",
        f"- Missing: {counts.get('missing', 0)}",
        f"- Inconclusive: {counts.get('inconclusive', 0)}",
        f"- Interface: {interface_status}",
        f"- Register map: {register_check['status']}",
        f"- Clock/reset: {clock_reset_check['status']}",
        f"- Feature contracts: {feature_contract_check['status']} ({feature_contract_check['passed']}/{feature_contract_check['checked']} statically bound)",
        "",
        "## Missing Or Partial Requirements",
    ]
    for item in requirement_results:
        if item["status"] != "matched":
            md_lines.append(f"- {item['id']} [{item['status']}]: {item['requirement']}")
    if not any(item["status"] != "matched" for item in requirement_results):
        md_lines.append("- None reported.")

    report_text = json.dumps(report, indent=2)
    md_text = "\n".join(md_lines).strip() + "\n"
    if not state.get("_spec2rtl_embedded"):
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/spec2rtl", "spec2rtl_conformance.json", report_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/spec2rtl", "SPEC2RTL_CONFORMANCE.md", md_text)

    state["spec2rtl_conformance"] = report
    state["spec2rtl_status"] = status
    state["spec2rtl_summary"] = counts
    state["status"] = f"Spec2RTL conformance {status}"
    return state
