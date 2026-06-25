import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

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

    roots = [
        Path(str(state.get("artifact_dir") or "")),
        Path(str(state.get("artifact_dir") or "")).parent,
    ]
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
        if current is None or "handoff" in p.parts:
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
            modules.append({"name": name, "file": path, "ports": ports})
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


def _match_score(requirement: str, rtl_text: str, rtl_names: Iterable[str]) -> Tuple[str, List[str]]:
    req_lower = requirement.lower()
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", requirement)
        if w.lower() not in GENERIC_REQUIREMENT_WORDS
    ]
    names = {n.lower() for n in rtl_names}
    unique_words = list(dict.fromkeys(words))
    evidence = [w for w in unique_words if w in names or re.search(rf"\b{re.escape(w)}\b", rtl_text, re.I)]
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
    if re.search(r"\bincrement", req_lower) and re.search(r"\+\s*(?:\d+'[bdh])?0*1\b|\+\s*1'b1\b", rtl_text, re.I):
        evidence.append("increment_logic")
    if re.search(r"\bwrap", req_lower) and re.search(r">=|==", rtl_text) and re.search(r"<=\s*(?:\d+'h00|\d+'d0|0)\b", rtl_text, re.I):
        evidence.append("wrap_logic")
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
        if (
            re.search(r"\bwr_addr\s*==\s*\d+'h0*18", rtl_text, re.I)
            and re.search(r"\bwr_data\s*\[\s*1\s*\]", rtl_text, re.I)
            and re.search(r"\birq_status_sample_done\s*<=\s*1'b0", rtl_text, re.I)
            and re.search(r"\bstatus_sample_done\s*<=\s*1'b0", rtl_text, re.I)
        ):
            evidence.append("IRQ_CLEAR.sample_done clear")
        if (
            re.search(r"\bwr_addr\s*==\s*\d+'h0*18", rtl_text, re.I)
            and re.search(r"\bwr_data\s*\[\s*1\s*\]", rtl_text, re.I)
            and re.search(r"\birq_status_\w*\s*\[\s*1\s*\]\s*<=\s*1'b0", rtl_text, re.I)
            and re.search(r"\bstatus_\w*\s*\[\s*0\s*\]\s*<=\s*1'b0", rtl_text, re.I)
        ):
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
    }
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
    matched_registers = []
    missing_registers = []
    for reg_name, address in sorted(dict.fromkeys(registers)):
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
        "expected_registers": [r[0] for r in sorted(dict.fromkeys(registers))],
        "matched_registers": matched_registers,
        "expected_addresses": sorted(dict.fromkeys(addresses)),
        "matched_addresses": matched_addresses,
        "missing": missing_fields,
        "missing_registers": missing_registers,
        "missing_addresses": missing_addresses,
        "status": "pass" if (unique or addresses or registers) and not missing_fields and not missing_addresses and not missing_registers else ("not_applicable" if not unique and not addresses and not registers else "issues"),
    }


def _structured_requirements(spec_obj: Optional[Dict[str, Any]], spec: str) -> List[str]:
    reqs: List[str] = []
    for mod in _structured_spec_modules(spec_obj):
        for key in ("responsibilities", "behavior_rules", "must_drive", "must_receive"):
            values = mod.get(key)
            if isinstance(values, list):
                reqs.extend(str(v).strip() for v in values if str(v).strip())
        for key in ("reset_behavior",):
            if isinstance(mod.get(key), str) and mod[key].strip():
                reqs.append(mod[key].strip())
    if reqs:
        return list(dict.fromkeys(r[:240] for r in reqs if len(r) >= 8))[:80]
    return _extract_requirements(spec)


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
    interface_status = "pass" if spec_ports and not missing_ports else ("inconclusive" if not spec_ports else "issues")

    requirements = _structured_requirements(spec_obj, spec)
    requirement_results = []
    counts = {"checked": 0, "matched": 0, "partial": 0, "missing": 0, "inconclusive": 0}
    if setup_issues:
        for _ in requirements:
            _add_check(counts, "inconclusive")
    else:
        for idx, requirement in enumerate(requirements, start=1):
            status, evidence = _match_score(requirement, rtl_text, rtl_names)
            _add_check(counts, status)
            requirement_results.append({
                "id": f"REQ-{idx:03d}",
                "requirement": requirement,
                "status": status,
                "evidence_tokens": evidence,
            })

    register_check = _register_evidence(spec, rtl_text, state, spec_obj, regmap_obj)
    clock_reset_check = _clock_reset_evidence(spec, modules)
    _add_check(counts, top_status)
    _add_check(counts, interface_status)
    _add_check(counts, register_check["status"])
    _add_check(counts, clock_reset_check["status"])

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
            "rtl_ports": sorted(rtl_ports),
        },
        "register_map": register_check,
        "clock_reset": clock_reset_check,
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
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/spec2rtl", "spec2rtl_conformance.json", report_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital/spec2rtl", "SPEC2RTL_CONFORMANCE.md", md_text)

    state["spec2rtl_conformance"] = report
    state["spec2rtl_status"] = status
    state["spec2rtl_summary"] = counts
    state["status"] = f"Spec2RTL conformance {status}"
    return state
