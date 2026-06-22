import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.runtime import AgentContext, AgentResult, AgentStatus, execute_agent
from tooling.profiles import profile_summary
from tooling.runner import run_tool, tool_available
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Arch2RTL Dashboard Agent"


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _read_json(value: Any) -> Dict[str, Any]:
    try:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and os.path.exists(value):
            obj = json.loads(Path(value).read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _strip_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _prefer_handoff_rtl(files: List[str]) -> List[str]:
    by_basename: Dict[str, str] = {}
    for path in files:
        name = Path(path).name
        current = by_basename.get(name)
        if current is None:
            by_basename[name] = path
            continue
        current_is_handoff = "handoff" in Path(current).parts
        path_is_handoff = "handoff" in Path(path).parts
        if path_is_handoff and not current_is_handoff:
            by_basename[name] = path
    return sorted(by_basename.values())


def _collect_rtl_files(workflow_dir: str, state: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for key in ("rtl_files", "artifact_list"):
        value = state.get(key)
        if isinstance(value, list):
            files.extend(str(p) for p in value if isinstance(p, str) and p.lower().endswith((".v", ".sv")))
    for root in (Path(workflow_dir) / "rtl", Path(workflow_dir) / "handoff"):
        if root.exists():
            files.extend(str(p) for p in root.rglob("*") if p.suffix.lower() in {".v", ".sv"})
    existing = sorted(str(Path(p)) for p in dict.fromkeys(files) if Path(p).exists())
    return _prefer_handoff_rtl(existing)


def _existing_rtl_files(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    files = []
    for value in values:
        if isinstance(value, str) and value.lower().endswith((".v", ".sv")) and Path(value).exists():
            files.append(str(Path(value)))
    return sorted(dict.fromkeys(files))


def _mbist_summary(workflow_dir: str, state: Dict[str, Any]) -> Dict[str, Any]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    summary = _read_json(digital.get("mbist_rtl_insertion"))
    if summary:
        return summary
    path = Path(workflow_dir) / "digital" / "mbist_rtl_insertion" / "mbist_rtl_insertion_summary.json"
    return _read_json(str(path))


def _functional_rtl_files(workflow_dir: str, state: Dict[str, Any], mbist: Dict[str, Any]) -> List[str]:
    deduped = mbist.get("deduped_functional_rtl") if isinstance(mbist.get("deduped_functional_rtl"), dict) else {}
    for source in (deduped.get("files"), mbist.get("rtl_files_scanned")):
        files = _existing_rtl_files(source)
        if files:
            return files

    root = Path(workflow_dir) / "rtl"
    if root.exists():
        return sorted(str(p) for p in root.rglob("*") if p.suffix.lower() in {".v", ".sv"})
    return []


def _module_header(text: str, module_name: str) -> str:
    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b(.*?)\)\s*;", text, re.DOTALL)
    return m.group(0) if m else ""


def _parse_modules(rtl_files: List[str]) -> List[Dict[str, Any]]:
    modules: List[Dict[str, Any]] = []
    seen_modules = set()
    mod_pat = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b(.*?)(?=\bendmodule\b)", re.DOTALL)
    port_decl = re.compile(
        r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s*)?(\[[^\]]+\])?\s*([^;]+);",
        re.IGNORECASE,
    )
    ansi_port = re.compile(
        r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:signed\s*)?(\[[^\]]+\])?\s*([A-Za-z_][A-Za-z0-9_$]*)",
        re.IGNORECASE,
    )
    for path in rtl_files:
        text = _strip_comments(_read_text(path))
        for m in mod_pat.finditer(text):
            name = m.group(1)
            if name in seen_modules:
                continue
            seen_modules.add(name)
            body = m.group(2)
            ports: List[Dict[str, Any]] = []
            seen = set()
            for rx_source in (_module_header(text, name), body):
                rx = ansi_port if rx_source.startswith("module") else port_decl
                for pm in rx.finditer(rx_source):
                    direction = pm.group(1).lower()
                    width = pm.group(2) or ""
                    names = [pm.group(3)] if rx is ansi_port else re.split(r",", pm.group(3))
                    for raw_name in names:
                        port_name = re.sub(r"=.*$", "", raw_name).strip()
                        port_name = re.sub(r"\[[^\]]+\]", "", port_name).strip()
                        if not port_name or not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", port_name):
                            continue
                        key = (direction, port_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        ports.append({"name": port_name, "direction": direction, "width": width.strip() or "1"})
            modules.append({
                "name": name,
                "file": path,
                "input_count": sum(_range_width(str(p.get("width") or "1")) for p in ports if p["direction"] == "input"),
                "output_count": sum(_range_width(str(p.get("width") or "1")) for p in ports if p["direction"] == "output"),
                "inout_count": sum(_range_width(str(p.get("width") or "1")) for p in ports if p["direction"] == "inout"),
                "input_port_count": sum(1 for p in ports if p["direction"] == "input"),
                "output_port_count": sum(1 for p in ports if p["direction"] == "output"),
                "inout_port_count": sum(1 for p in ports if p["direction"] == "inout"),
                "ports": ports,
            })
    return modules


def _infer_clock_reset(modules: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    ports = [p for m in modules for p in m.get("ports", [])]
    clocks = list(dict.fromkeys(str(x) for x in state.get("clock_ports", []) if isinstance(x, str)))
    resets = list(dict.fromkeys(str(x) for x in state.get("reset_ports", []) if isinstance(x, str)))
    if not clocks:
        clocks = [p["name"] for p in ports if p["direction"] == "input" and re.search(r"(^|_)(clk|clock)($|_)", p["name"], re.I)]
    if not resets:
        resets = [p["name"] for p in ports if p["direction"] == "input" and re.search(r"(^|_)(rst|reset|reset_n|rst_n)($|_)", p["name"], re.I)]
    return {
        "clock_names": sorted(dict.fromkeys(clocks)),
        "reset_names": sorted(dict.fromkeys(resets)),
        "primary_clock": clocks[0] if clocks else None,
        "primary_reset": resets[0] if resets else None,
    }


def _range_width(width: str) -> int:
    if not width:
        return 1
    m = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", width)
    if not m:
        return 1
    return abs(int(m.group(1)) - int(m.group(2))) + 1


def _array_count(suffix: str) -> int:
    count = 1
    for hi, lo in re.findall(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", suffix or ""):
        count *= abs(int(hi) - int(lo)) + 1
    return count


def _declared_storage_bits(text: str) -> Tuple[Dict[str, int], Dict[str, int]]:
    decls: Dict[str, int] = {}
    memories: Dict[str, int] = {}
    decl_pat = re.compile(r"\b(?:reg|logic)\b\s*(?:signed\s*)?(\[[^\]]+\])?\s*([^;]+);", re.IGNORECASE)
    for dm in decl_pat.finditer(text):
        packed = dm.group(1) or ""
        width = _range_width(packed)
        for raw in dm.group(2).split(","):
            item = re.sub(r"=.*$", "", raw).strip()
            nm = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)\s*((?:\[[^\]]+\]\s*)*)$", item)
            if not nm:
                continue
            name = nm.group(1)
            unpacked_count = _array_count(nm.group(2))
            bit_count = width * unpacked_count
            decls[name] = bit_count
            if unpacked_count > 1:
                memories[name] = bit_count
    return decls, memories


def _storage_target_name(lhs: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", lhs or "").strip()


def _count_storage(rtl_files: List[str]) -> Dict[str, Any]:
    flop_targets = set()
    latch_targets = set()
    memory_targets = set()
    flop_bits_by_target: Dict[str, int] = {}
    latch_bits_by_target: Dict[str, int] = {}
    memory_bits_by_target: Dict[str, int] = {}
    posedge_blocks = 0
    negedge_blocks = 0
    always_comb_blocks = 0
    for path in rtl_files:
        text = _strip_comments(_read_text(path))
        decl_bits, memory_bits = _declared_storage_bits(text)
        for block in re.findall(r"\balways_ff\b\s*@\s*\((.*?)\)(.*?)(?=\balways|\bendmodule\b)", text, re.DOTALL):
            sens, body = block
            if re.search(r"\bposedge\b", sens):
                posedge_blocks += 1
            if re.search(r"\bnegedge\b", sens):
                negedge_blocks += 1
            for lhs in re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*<=", body):
                target = _storage_target_name(lhs)
                if target in memory_bits:
                    memory_targets.add(target)
                    memory_bits_by_target[target] = max(memory_bits_by_target.get(target, 0), memory_bits[target])
                    continue
                flop_targets.add(target)
                flop_bits_by_target[target] = max(flop_bits_by_target.get(target, 0), decl_bits.get(target, 1))
        for block in re.findall(r"\balways\s*@\s*\((.*?)\)(.*?)(?=\balways|\bendmodule\b)", text, re.DOTALL):
            sens, body = block
            if re.search(r"\b(posedge|negedge)\b", sens):
                if re.search(r"\bposedge\b", sens):
                    posedge_blocks += 1
                if re.search(r"\bnegedge\b", sens):
                    negedge_blocks += 1
                for lhs in re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*<=", body):
                    target = _storage_target_name(lhs)
                    if target in memory_bits:
                        memory_targets.add(target)
                        memory_bits_by_target[target] = max(memory_bits_by_target.get(target, 0), memory_bits[target])
                        continue
                    flop_targets.add(target)
                    flop_bits_by_target[target] = max(flop_bits_by_target.get(target, 0), decl_bits.get(target, 1))
            elif "<=" in body:
                for lhs in re.findall(r"\b([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*<=", body):
                    target = _storage_target_name(lhs)
                    if target in memory_bits:
                        memory_targets.add(target)
                        memory_bits_by_target[target] = max(memory_bits_by_target.get(target, 0), memory_bits[target])
                        continue
                    latch_targets.add(target)
                    latch_bits_by_target[target] = max(latch_bits_by_target.get(target, 0), decl_bits.get(target, 1))
        always_comb_blocks += len(re.findall(r"\balways_comb\b|\balways\s*@\s*\*", text))
        for lhs in re.findall(r"\balways_latch\b.*?\b([A-Za-z_][A-Za-z0-9_$]*)\s*<=", text, re.DOTALL):
            target = _storage_target_name(lhs)
            if target in memory_bits:
                memory_targets.add(target)
                memory_bits_by_target[target] = max(memory_bits_by_target.get(target, 0), memory_bits[target])
                continue
            latch_targets.add(target)
            latch_bits_by_target[target] = max(latch_bits_by_target.get(target, 0), decl_bits.get(target, 1))
    flop_bit_count = sum(flop_bits_by_target.values()) or len(flop_targets)
    latch_bit_count = sum(latch_bits_by_target.values()) or len(latch_targets)
    return {
        "flipflop_count": flop_bit_count,
        "latch_count": latch_bit_count,
        "memory_bit_count": sum(memory_bits_by_target.values()),
        "memory_target_count": len(memory_targets),
        "storage_target_count": len(flop_targets),
        "latch_target_count": len(latch_targets),
        "sequential_blocks": posedge_blocks + negedge_blocks,
        "posedge_blocks": posedge_blocks,
        "negedge_blocks": negedge_blocks,
        "combinational_blocks": always_comb_blocks,
        "sampled_flipflop_targets": sorted(flop_targets)[:50],
        "sampled_latch_targets": sorted(latch_targets)[:50],
        "sampled_memory_targets": sorted(memory_targets)[:50],
    }


def _find_sdc_files(workflow_dir: str, state: Dict[str, Any]) -> List[str]:
    files = []
    for key in ("digital_sdc_path", "sdc_path", "constraints_sdc"):
        value = state.get(key)
        if isinstance(value, str) and value.endswith(".sdc") and os.path.exists(value):
            files.append(value)
    root = Path(workflow_dir)
    if root.exists():
        files.extend(str(p) for p in root.rglob("*.sdc"))
    return sorted(dict.fromkeys(files))


def _timing_summary(workflow_dir: str, state: Dict[str, Any], storage: Dict[str, Any]) -> Dict[str, Any]:
    sdc_files = _find_sdc_files(workflow_dir, state)
    text = "\n".join(_read_text(p) for p in sdc_files)
    multicycle = len(re.findall(r"\bset_multicycle_path\b", text))
    false_paths = len(re.findall(r"\bset_false_path\b", text))
    io_paths = len(re.findall(r"\bset_(?:input|output)_delay\b", text))
    full_cycle_classes = 0
    if storage["flipflop_count"]:
        full_cycle_classes += 1
    if io_paths:
        full_cycle_classes += io_paths
    elif state.get("port_list") or storage["flipflop_count"]:
        full_cycle_classes += 2
    half_cycle_candidates = storage["negedge_blocks"]
    return {
        "sdc_file_count": len(sdc_files),
        "sdc_files": sdc_files,
        "full_cycle_path_count": full_cycle_classes,
        "half_cycle_path_count": half_cycle_candidates,
        "multicycle_path_count": multicycle,
        "false_path_count": false_paths,
        "basis": "SDC constraints plus RTL edge-triggered block inference",
    }


def _lint_summary(rtl_files: List[str], state: Dict[str, Any]) -> Dict[str, Any]:
    verilator_available = tool_available("verilator", state)
    if not rtl_files:
        return {"status": "missing_rtl", "tool": "verilator", "available": verilator_available}
    if not verilator_available:
        return {"status": "tool_unavailable", "tool": "verilator", "available": False}
    result = run_tool(
        state,
        "rtl_lint",
        "verilator",
        ["--lint-only", "-Wall", "-Wno-fatal"] + rtl_files,
        timeout_sec=45,
        metadata={"agent": AGENT_NAME, "stage": "arch2rtl_dashboard"},
    )
    combined = f"{result.stdout}\n{result.stderr}"
    error_count = len(re.findall(r"%Error", combined))
    warning_count = len(re.findall(r"%Warning", combined))
    if result.status in {"exception", "tool_unavailable"}:
        status = result.status
    elif result.returncode != 0 or error_count:
        status = "errors"
    elif warning_count:
        status = "warnings"
    else:
        status = "clean"
    return {
        "status": status,
        "tool": "verilator",
        "available": True,
        "profile_id": result.profile_id,
        "runner": result.runner,
        "returncode": result.returncode,
        "warning_count": warning_count,
        "error_count": error_count,
        "command": result.command,
        "stdout_tail": (result.stdout or "").splitlines()[-80:],
        "stderr_tail": (result.stderr or "").splitlines()[-80:],
        "error": result.error,
        "run_result": result.to_dict(),
    }


def _regmap_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    regmap = _read_json(state.get("digital_regmap_json") or state.get("regmap_json"))
    registers = regmap.get("registers")
    if not isinstance(registers, list):
        registers = (regmap.get("regmap") or {}).get("registers") if isinstance(regmap.get("regmap"), dict) else []
    if not isinstance(registers, list):
        registers = []
    return {"register_count": len(registers), "registers": registers[:12]}


def _scope_report(name: str, rtl_files: List[str], top_name: str, state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    modules = _parse_modules(rtl_files)
    top_module = next((m for m in modules if m["name"] == top_name), modules[0] if modules else {})
    storage = _count_storage(rtl_files)
    interface = {
        "input_count": int(top_module.get("input_count") or 0),
        "output_count": int(top_module.get("output_count") or 0),
        "inout_count": int(top_module.get("inout_count") or 0),
        "input_port_count": int(top_module.get("input_port_count") or 0),
        "output_port_count": int(top_module.get("output_port_count") or 0),
        "inout_port_count": int(top_module.get("inout_port_count") or 0),
        "count_basis": "bits",
        "ports": top_module.get("ports") or [],
    }
    return {
        "name": name,
        "rtl_file_count": len(rtl_files),
        "module_count": len(modules),
        "modules": modules,
        "interface": interface,
        "storage": storage,
        "timing": _timing_summary(workflow_dir, state, storage),
    }


def _markdown(report: Dict[str, Any]) -> str:
    lint = report["lint"]
    storage = report["storage"]
    timing = report["timing"]
    io = report["interface"]
    return "\n".join([
        "# Arch2RTL Evidence Dashboard",
        "",
        f"- Lint: {lint.get('status')}",
        f"- Flip-flops: {storage.get('flipflop_count')}",
        f"- Latches: {storage.get('latch_count')}",
        f"- Full-cycle paths: {timing.get('full_cycle_path_count')}",
        f"- Half-cycle paths: {timing.get('half_cycle_path_count')}",
        f"- Input bits: {io.get('input_count')}",
        f"- Output bits: {io.get('output_count')}",
        f"- Input ports: {io.get('input_port_count')}",
        f"- Output ports: {io.get('output_port_count')}",
        f"- Clock: {report['clock_reset'].get('primary_clock') or 'not inferred'}",
        f"- Reset: {report['clock_reset'].get('primary_reset') or 'not inferred'}",
        f"- Modules: {report.get('module_count')}",
        f"- RTL files: {report.get('rtl_file_count')}",
        "",
    ])


def _run(context: AgentContext) -> AgentResult:
    state = context.state
    workflow_id = context.workflow_id
    workflow_dir = str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = Path(workflow_dir) / "digital"
    out_dir.mkdir(parents=True, exist_ok=True)

    package_rtl_files = _collect_rtl_files(workflow_dir, state)
    mbist = _mbist_summary(workflow_dir, state)
    functional_rtl_files = _functional_rtl_files(workflow_dir, state, mbist)
    rtl_files = functional_rtl_files or package_rtl_files
    modules = _parse_modules(rtl_files)
    top_name = str(state.get("top_module") or (modules[0]["name"] if modules else "top"))
    top_module = next((m for m in modules if m["name"] == top_name), modules[0] if modules else {})
    storage = _count_storage(rtl_files)
    clock_reset = _infer_clock_reset(modules, state)
    interface = {
        "input_count": int(top_module.get("input_count") or 0),
        "output_count": int(top_module.get("output_count") or 0),
        "inout_count": int(top_module.get("inout_count") or 0),
        "input_port_count": int(top_module.get("input_port_count") or 0),
        "output_port_count": int(top_module.get("output_port_count") or 0),
        "inout_port_count": int(top_module.get("inout_port_count") or 0),
        "count_basis": "bits",
        "ports": top_module.get("ports") or [],
    }
    report = {
        "type": "arch2rtl_dashboard",
        "version": "1.0",
        "top_module": top_name,
        "rtl_file_count": len(rtl_files),
        "module_count": len(modules),
        "modules": modules,
        "interface": interface,
        "clock_reset": clock_reset,
        "storage": storage,
        "timing": _timing_summary(workflow_dir, state, storage),
        "lint": _lint_summary(rtl_files, state),
        "scopes": {
            "functional": _scope_report("functional", rtl_files, top_name, state, workflow_dir),
            "complete_package": _scope_report("complete_package", package_rtl_files, top_name, state, workflow_dir),
        },
        "metric_policy": {
            "primary_scope": "functional" if functional_rtl_files else "complete_package",
            "complete_package_note": "Complete package includes generated MBIST wrappers, algorithms, saboteur/model RTL, and staged SRAM behavioral models.",
            "storage_note": "Unpacked behavioral memory arrays are reported as memory_bit_count, not flipflop_count.",
        },
        "tooling": {
            "tool_profile": profile_summary(state),
            "artifact_policy": profile_summary(state).get("artifact_policy"),
        },
        "register_map": _regmap_summary(state),
        "handoff": {
            "package_dir": state.get("ip_package_dir"),
            "package_zip": state.get("ip_package_zip"),
        },
    }
    json_text = json.dumps(report, indent=2)
    md_text = _markdown(report)
    (out_dir / "arch2rtl_dashboard.json").write_text(json_text, encoding="utf-8")
    (out_dir / "ARCH2RTL_DASHBOARD.md").write_text(md_text, encoding="utf-8")
    tool_profile_text = json.dumps(profile_summary(state), indent=2)
    tool_summary = {
        "agent": AGENT_NAME,
        "tool_profile": profile_summary(state),
        "executions": [report["lint"].get("run_result")] if report["lint"].get("run_result") else [],
    }
    tool_summary_text = json.dumps(tool_summary, indent=2)
    (out_dir / "tool_profile_used.json").write_text(tool_profile_text, encoding="utf-8")
    (out_dir / "tool_execution_summary.json").write_text(tool_summary_text, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "arch2rtl_dashboard.json", json_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "ARCH2RTL_DASHBOARD.md", md_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tool_profile_used.json", tool_profile_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tool_execution_summary.json", tool_summary_text)
    return AgentResult(
        status="Arch2RTL dashboard generated",
        runtime_status=AgentStatus.SUCCESS,
        data={
            "arch2rtl_dashboard_json": str(out_dir / "arch2rtl_dashboard.json"),
            "arch2rtl_dashboard": report,
            "status": "Arch2RTL dashboard generated",
        },
        artifacts={"arch2rtl_dashboard.json": "digital/arch2rtl_dashboard.json"},
    )


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    context = AgentContext.from_state(state, AGENT_NAME)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state
