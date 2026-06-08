import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Spec2RTL Conformance Agent"
RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


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
    for key in ("spec_text", "spec", "requirements", "test_intent"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key in ("spec_json", "digital_spec_json"):
        value = state.get(key)
        if isinstance(value, dict):
            parts.append(json.dumps(value, indent=2))
    for key in ("spec_json_path", "digital_spec_json_path"):
        value = state.get(key)
        if isinstance(value, str) and Path(value).exists():
            parts.append(_read_text(value))
    return "\n\n".join(dict.fromkeys(parts))


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
            if name.lower() not in {"input", "output", "inout", "wire", "logic", "reg"}:
                ports.add(name)
    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]+\])", spec or ""):
        ports.add(name)
    return sorted(ports)


def _match_score(requirement: str, rtl_text: str, rtl_names: Iterable[str]) -> Tuple[str, List[str]]:
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", requirement)
        if w.lower() not in {"shall", "must", "should", "when", "input", "output", "register", "behavior"}
    ]
    names = {n.lower() for n in rtl_names}
    evidence = [w for w in dict.fromkeys(words) if w in names or re.search(rf"\b{re.escape(w)}\b", rtl_text, re.I)]
    if len(evidence) >= max(2, min(5, len(set(words)) // 2)):
        return "matched", evidence[:8]
    if evidence:
        return "partial", evidence[:8]
    return "missing", []


def _register_evidence(spec: str, rtl_text: str, state: Dict[str, Any]) -> Dict[str, Any]:
    regmap = state.get("regmap_json") or state.get("digital_regmap_json")
    fields: List[str] = []
    if isinstance(regmap, dict):
        raw = json.dumps(regmap)
        fields.extend(re.findall(r'"(?:name|field|field_name|register)"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"', raw))
    fields.extend(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:_reg|_cfg|_ctrl|_status))\b", spec or "", re.I))
    unique = sorted(dict.fromkeys(fields))
    matched = [f for f in unique if re.search(rf"\b{re.escape(f)}\b", rtl_text, re.I)]
    return {
        "expected": unique,
        "matched": matched,
        "missing": [f for f in unique if f not in matched],
        "status": "pass" if unique and len(matched) == len(unique) else ("not_applicable" if not unique else "issues"),
    }


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

    top_module = str(state.get("top_module") or "").strip()
    module_names = [m["name"] for m in modules]
    top_status = "pass" if top_module and top_module in module_names else ("inconclusive" if not top_module else "issues")

    spec_ports = _extract_spec_ports(spec)
    rtl_ports = {p["name"] for m in modules for p in m.get("ports", [])}
    matched_ports = [p for p in spec_ports if p in rtl_ports]
    missing_ports = [p for p in spec_ports if p not in rtl_ports]
    interface_status = "pass" if spec_ports and not missing_ports else ("inconclusive" if not spec_ports else "issues")

    requirements = _extract_requirements(spec)
    requirement_results = []
    counts = {"checked": 0, "matched": 0, "partial": 0, "missing": 0, "inconclusive": 0}
    if setup_issues:
        counts["inconclusive"] = len(requirements)
    else:
        for idx, requirement in enumerate(requirements, start=1):
            status, evidence = _match_score(requirement, rtl_text, rtl_names)
            counts["checked"] += 1
            counts[status] += 1
            requirement_results.append({
                "id": f"REQ-{idx:03d}",
                "requirement": requirement,
                "status": status,
                "evidence_tokens": evidence,
            })

    register_check = _register_evidence(spec, rtl_text, state)
    clock_reset_check = _clock_reset_evidence(spec, modules)
    if top_status == "issues":
        counts["missing"] += 1
        counts["checked"] += 1
    if interface_status == "issues":
        counts["missing"] += len(missing_ports)
        counts["checked"] += max(1, len(spec_ports))

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
