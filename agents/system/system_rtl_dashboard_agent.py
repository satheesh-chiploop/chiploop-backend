import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from agents.digital.digital_arch2rtl_dashboard_agent import (
    _count_storage,
    _infer_clock_reset,
    _parse_modules,
    _timing_summary,
)
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System RTL Evidence Dashboard Agent"
OUTPUT_SUBDIR = "system/dashboard"


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _existing_files(paths: List[str], workflow_dir: str) -> List[str]:
    out: List[str] = []
    for raw in paths:
        path = str(raw).strip()
        if not path or path == "(empty)":
            continue
        candidates = [Path(path)]
        if not os.path.isabs(path):
            candidates.insert(0, Path(workflow_dir) / path)
        for candidate in candidates:
            if candidate.exists() and candidate.suffix.lower() in {".v", ".sv"}:
                out.append(str(candidate.resolve()))
                break
    return sorted(dict.fromkeys(out))


def _collect_filelists(state: Dict[str, Any], workflow_dir: str) -> Dict[str, List[str]]:
    pkg = state.get("system_rtl_package") if isinstance(state.get("system_rtl_package"), dict) else {}
    filelists = pkg.get("filelists") if isinstance(pkg.get("filelists"), dict) else {}
    sim = _as_list(state.get("system_rtl_filelist_sim")) or _as_list(filelists.get("sim"))
    phys = _as_list(state.get("system_rtl_filelist_phys")) or _as_list(filelists.get("phys"))

    if not sim:
        fallback = Path(workflow_dir) / "system" / "integration" / "system_rtl_filelist_sim.txt"
        if fallback.exists():
            sim = [line.strip() for line in fallback.read_text(encoding="utf-8", errors="ignore").splitlines()]
    if not phys:
        fallback = Path(workflow_dir) / "system" / "integration" / "system_rtl_filelist_phys.txt"
        if fallback.exists():
            phys = [line.strip() for line in fallback.read_text(encoding="utf-8", errors="ignore").splitlines()]

    return {
        "sim": _existing_files(sim, workflow_dir),
        "phys": _existing_files(phys, workflow_dir),
    }


def _basename(path: str) -> str:
    return Path(path).name.lower()


def _path_parts(path: str) -> List[str]:
    return [part for part in path.replace("\\", "/").lower().split("/") if part]


def _is_soc_file(path: str) -> bool:
    name = _basename(path)
    text = path.replace("\\", "/").lower()
    parts = _path_parts(path)
    if "system" in parts and "integration" in parts and name.endswith((".sv", ".v")):
        return True
    return name.startswith("soc_top") or "system/integration/soc_top" in text


def _is_analog_file(path: str) -> bool:
    text = path.replace("\\", "/").lower()
    name = _basename(path)
    parts = _path_parts(path)
    if "analog" in parts:
        return True
    if "rtl" in parts or "digital" in parts or _is_digital_file(path):
        return False
    analog_tokens = (
        "analog",
        "macro",
        "ams",
        "behavioral",
        "adc",
        "dac",
        "pll",
        "ldo",
        "bandgap",
        "opamp",
    )
    return any(token in text or token in name for token in analog_tokens)


def _is_digital_file(path: str) -> bool:
    name = _basename(path)
    parts = _path_parts(path)
    if "rtl" in parts or "digital" in parts:
        return True
    digital_tokens = ("digital", "controller", "ctrl", "regs", "filter", "irq", "monitor")
    return any(token in name for token in digital_tokens)


def _scope_files(filelists: Dict[str, List[str]]) -> Dict[str, List[str]]:
    merged = sorted(dict.fromkeys(filelists.get("sim", []) + filelists.get("phys", [])))
    soc = [path for path in merged if _is_soc_file(path)]
    analog = [path for path in merged if path not in soc and _is_analog_file(path)]
    digital = [path for path in merged if path not in soc and path not in analog and _is_digital_file(path)]
    digital.extend(path for path in merged if path not in soc and path not in analog and path not in digital)
    digital = sorted(dict.fromkeys(digital))
    return {
        "system": merged,
        "soc": soc,
        "digital": digital,
        "analog": analog,
    }


def _top_for_scope(scope: str, modules: List[Dict[str, Any]], state: Dict[str, Any]) -> str:
    if scope == "soc":
        return str(state.get("soc_top_sim_module") or state.get("soc_top_phys_module") or (modules[0].get("name") if modules else "soc_top"))
    if scope == "digital":
        return str(state.get("top_module") or (modules[0].get("name") if modules else "digital_block"))
    if scope == "analog":
        return str(modules[0].get("name") if modules else "analog_macro")
    return str(state.get("soc_top_sim_module") or (modules[0].get("name") if modules else "system"))


def _compile_status(state: Dict[str, Any]) -> Dict[str, Any]:
    compile_summary = state.get("system_full_compile_summary")
    if not isinstance(compile_summary, dict):
        path = Path(str(state.get("workflow_dir") or "")) / "system" / "integration" / "system_full_compile_summary.json"
        if path.exists():
            try:
                compile_summary = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                compile_summary = {}
    sim = compile_summary.get("sim", {}) if isinstance(compile_summary, dict) else {}
    phys = compile_summary.get("phys", {}) if isinstance(compile_summary, dict) else {}
    sim_ok = bool(sim.get("iverilog_ok_pass2", sim.get("iverilog_ok_pass1", False))) and bool(sim.get("verilator_ok_pass2", sim.get("verilator_ok_pass1", False)))
    return {
        "sim": "pass" if sim_ok else "fail",
        "phys": "skipped" if phys.get("skipped") else ("pass" if phys else "not produced"),
        "phys_reason": phys.get("reason"),
    }


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _digital_lint_status(workflow_dir: str) -> str:
    summary = Path(workflow_dir) / "rtl" / "rtl_agent_summary.txt"
    if summary.exists():
        text = summary.read_text(encoding="utf-8", errors="ignore").lower()
        if "verilator lint: pass" in text and "icarus compile: pass" in text:
            return "pass"
        if "verilator lint: fail" in text or "icarus compile: fail" in text:
            return "fail"

    lint_reports = [
        Path(workflow_dir) / "rtl_lint_report.json",
        Path(workflow_dir) / "digital" / "rtl_lint_report.json",
    ]
    for report_path in lint_reports:
        report = _read_json(report_path)
        if not report:
            continue
        status = str(report.get("status") or report.get("lint") or "").lower()
        if status in {"pass", "clean"}:
            return "pass"
        if status in {"fail", "failed", "error"}:
            return "fail"

    return "not produced"


def _analog_lint_status(workflow_dir: str) -> str:
    analog_dir = Path(workflow_dir) / "analog"
    summaries = sorted(analog_dir.glob("*_compile_summary.json")) if analog_dir.exists() else []
    if not summaries:
        return "not produced"
    saw_clean = False
    for summary_path in summaries:
        summary = _read_json(summary_path)
        if not summary:
            continue
        failed = bool(summary.get("iverilog_failed")) or bool(summary.get("verilator_fatal")) or int(summary.get("issue_count") or 0) > 0
        if failed:
            return "fail"
        saw_clean = True
    return "pass" if saw_clean else "not produced"


def _lint_summary(state: Dict[str, Any], workflow_dir: str) -> Dict[str, str]:
    compile_status = _compile_status(state)
    digital = _digital_lint_status(workflow_dir)
    analog = _analog_lint_status(workflow_dir)
    soc = compile_status["sim"]
    parts = [digital, analog, soc]
    if any(status == "fail" for status in parts):
        system = "fail"
    elif all(status == "pass" for status in parts):
        system = "pass"
    elif any(status == "not produced" for status in parts):
        system = "not produced"
    else:
        system = "partial"
    return {
        "digital": digital,
        "analog": analog,
        "soc": soc,
        "system": system,
    }


def _scope_report(scope: str, rtl_files: List[str], state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    modules = _parse_modules(rtl_files)
    top_name = _top_for_scope(scope, modules, state)
    top_module = next((m for m in modules if m.get("name") == top_name), modules[0] if modules else {})
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
    lint_statuses = _lint_summary(state, workflow_dir)
    lint_status = lint_statuses.get(scope, lint_statuses.get("system", "not produced"))
    return {
        "scope": scope,
        "top_module": top_name,
        "rtl_file_count": len(rtl_files),
        "module_count": len(modules),
        "modules": modules,
        "interface": interface,
        "clock_reset": _infer_clock_reset(modules, state),
        "storage": storage,
        "timing": _timing_summary(workflow_dir, state, storage),
        "lint": {"status": lint_status, "basis": "Compile/lint evidence from RTL generation, analog model compile, and System Top Assembly."},
    }


def _markdown(report: Dict[str, Any]) -> str:
    lines = ["# System RTL Evidence Dashboard", ""]
    for scope, item in (report.get("scopes") or {}).items():
        interface = item.get("interface") or {}
        storage = item.get("storage") or {}
        timing = item.get("timing") or {}
        lines.extend([
            f"## {scope.title()}",
            f"- Top module: {item.get('top_module')}",
            f"- RTL files: {item.get('rtl_file_count')}",
            f"- Modules: {item.get('module_count')}",
            f"- Input bits: {interface.get('input_count')}",
            f"- Output bits: {interface.get('output_count')}",
            f"- Flip-flops: {storage.get('flipflop_count')}",
            f"- Latches: {storage.get('latch_count')}",
            f"- Full-cycle paths: {timing.get('full_cycle_path_count')}",
            f"- Half-cycle paths: {timing.get('half_cycle_path_count')}",
            "",
        ])
    return "\n".join(lines)


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")

    print(f"\nRunning {AGENT_NAME}")

    filelists = _collect_filelists(state, workflow_dir)
    scoped_files = _scope_files(filelists)
    scopes = {
        scope: _scope_report(scope, files, state, workflow_dir)
        for scope, files in scoped_files.items()
    }

    report = {
        "type": "system_rtl_dashboard",
        "version": "1.0",
        "filelists": {
            "sim_count": len(filelists["sim"]),
            "phys_count": len(filelists["phys"]),
        },
        "compile": _compile_status(state),
        "lint_summary": _lint_summary(state, workflow_dir),
        "scopes": scopes,
        "classification": {
            "basis": "filelist path/module-name classification; SoC tops separated from analog macro and digital RTL files",
            "analog_patterns": ["analog", "macro", "ams", "behavioral", "sensor", "temp", "adc", "dac", "pll", "ldo", "bandgap", "opamp"],
        },
    }
    json_text = json.dumps(report, indent=2)
    md_text = _markdown(report)

    out_dir = Path(workflow_dir) / OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "system_rtl_dashboard.json").write_text(json_text, encoding="utf-8")
    (out_dir / "SYSTEM_RTL_DASHBOARD.md").write_text(md_text, encoding="utf-8")

    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, "system_rtl_dashboard.json", json_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, "SYSTEM_RTL_DASHBOARD.md", md_text)

    state["system_rtl_dashboard"] = report
    state["system_rtl_dashboard_json"] = str(out_dir / "system_rtl_dashboard.json")
    state["status"] = "System RTL dashboard generated"
    return state
