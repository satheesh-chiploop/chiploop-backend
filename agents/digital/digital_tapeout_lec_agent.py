import glob
import json
import os
import re
import shutil
from datetime import datetime

from agents.digital.digital_logic_equivalence_agent import (
    AGENT_NAME as SYNTH_LEC_AGENT_NAME,
    _classify,
    _failure_reason,
    _generated_stdcell_model,
    _liberty_candidates,
    _missing_stdcell_models,
    _prepare_golden_rtl_for_yosys,
    _prepare_stdcell_models_for_yosys,
    _rtl_sources,
    _stdcell_verilog_candidates,
    _top_module,
    _write_text,
    _yosys_script,
)
from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Tapeout Logic Equivalence Check Agent"
PHYSICAL_ONLY_TOP_PORTS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "vccd1", "vssd1", "vdda1", "vssa1"}


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _latest_run_dir(stage_work_dir: str | None) -> str | None:
    if not stage_work_dir:
        return None
    runs_dir = os.path.join(stage_work_dir, "runs")
    if not os.path.isdir(runs_dir):
        return None
    dirs = [
        os.path.join(runs_dir, name)
        for name in os.listdir(runs_dir)
        if os.path.isdir(os.path.join(runs_dir, name))
    ]
    if not dirs:
        return None
    dirs.sort(key=lambda path: os.path.getmtime(path))
    return dirs[-1]


def _candidate_final_netlists(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    tapeout = digital.get("tapeout") if isinstance(digital.get("tapeout"), dict) else {}
    route = digital.get("route") if isinstance(digital.get("route"), dict) else {}
    candidates: list[str] = []

    for key in ("final_netlist", "netlist", "gate_netlist"):
        value = tapeout.get(key)
        if isinstance(value, str) and os.path.exists(value):
            candidates.append(value)

    for run_dir in (
        tapeout.get("openlane_run_dir"),
        _latest_run_dir(os.path.join(digital.get("digital_run_work_dir") or state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work"), "tapeout")),
        route.get("openlane_run_dir"),
        _latest_run_dir(os.path.join(digital.get("digital_run_work_dir") or state.get("digital_run_work_dir") or os.path.join(workflow_dir, "digital", "run_work"), "route")),
    ):
        if not isinstance(run_dir, str) or not os.path.isdir(run_dir):
            continue
        patterns = [
            "final/**/*.v",
            "**/final/**/*.v",
            "**/*pnl*.v",
            "**/*nl*.v",
            "**/*route*.v",
            "**/*.v",
        ]
        for pattern in patterns:
            hits = sorted(glob.glob(os.path.join(run_dir, pattern), recursive=True))
            candidates.extend(hits)
            if hits:
                break

    candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "digital", "tapeout", "netlist", "*.v"))))
    candidates.extend(sorted(glob.glob(os.path.join(workflow_dir, "digital", "route", "netlist", "*.v"))))

    out: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        ap = os.path.abspath(path)
        base = os.path.basename(ap).lower()
        if ap in seen or not os.path.exists(ap):
            continue
        if "synth" in base and ("route" not in base and "final" not in base and "pnl" not in base):
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _select_final_netlist(state: dict, workflow_dir: str) -> str | None:
    candidates = _candidate_final_netlists(state, workflow_dir)
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda path: (
            0 if "final" in path.lower() else 1,
            0 if any(token in os.path.basename(path).lower() for token in ("pnl", "route", "powered")) else 1,
            len(path),
        ),
    )
    return ranked[0]


def _top_ports(netlist: str | None, top: str) -> set[str]:
    if not netlist or not os.path.exists(netlist):
        return set()
    try:
        text = open(netlist, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return set()
    match = re.search(rf"\bmodule\s+{re.escape(top)}\s*\((?P<ports>.*?)\)\s*;", text, flags=re.DOTALL)
    if not match:
        return set()
    ports: set[str] = set()
    for raw_port in match.group("ports").replace("\n", " ").split(","):
        tokens = raw_port.strip().lstrip("\\").split()
        if not tokens:
            continue
        ports.add(tokens[-1])
    return ports


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "tapeout_lec")
    logs_dir = os.path.join(stage_dir, "logs")
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(logs_dir)
    _ensure_dir(input_dir)

    rtl_files = _rtl_sources(state, workflow_dir)
    final_netlist = _select_final_netlist(state, workflow_dir)
    staged_netlist = None
    if final_netlist:
        staged_netlist = os.path.join(input_dir, os.path.basename(final_netlist))
        shutil.copy2(final_netlist, staged_netlist)

    top = _top_module(state, rtl_files, staged_netlist)
    ignored_gate_ports = sorted((_top_ports(staged_netlist, top) - _top_ports(rtl_files[0] if rtl_files else None, top)) & PHYSICAL_ONLY_TOP_PORTS)
    yosys = tool_path("yosys", state)
    liberty_files = _liberty_candidates(state, workflow_dir)
    stdcell_verilog = _stdcell_verilog_candidates(state, workflow_dir)
    generated_stdcell_model = _generated_stdcell_model(staged_netlist, stage_dir)
    yosys_stdcell_verilog = [generated_stdcell_model] if generated_stdcell_model else _prepare_stdcell_models_for_yosys(stdcell_verilog, stage_dir)
    missing_stdcell_models = _missing_stdcell_models(staged_netlist, yosys_stdcell_verilog)

    script_path = os.path.join(stage_dir, "yosys_tapeout_lec.ys")
    log_path = os.path.join(logs_dir, "yosys_tapeout_lec.log")
    summary_path = os.path.join(stage_dir, "tapeout_lec_summary.json")
    report_path = os.path.join(stage_dir, "tapeout_lec_report.md")

    prepared_rtl_files, golden_macro_stubs = _prepare_golden_rtl_for_yosys(rtl_files, staged_netlist, stage_dir, top) if rtl_files and staged_netlist else (rtl_files, [])
    has_inputs = bool(prepared_rtl_files and staged_netlist and yosys and yosys_stdcell_verilog and not missing_stdcell_models)
    script = _yosys_script(
        prepared_rtl_files,
        staged_netlist,
        top,
        yosys_stdcell_verilog,
        gate_ignore_ports=ignored_gate_ports,
        gate_blackbox_verilog=golden_macro_stubs,
    ) if rtl_files and staged_netlist else "# Missing RTL or final tapeout netlist; post-tapeout LEC not run.\n"
    script = script.replace(f"# Auto-generated by {SYNTH_LEC_AGENT_NAME}", f"# Auto-generated by {AGENT_NAME}")
    _write_text(script_path, script)

    rc = None
    log = ""
    if has_inputs:
        proc = run_command(state, "tapeout_logic_equivalence", ["yosys", "-s", script_path], cwd=stage_dir, timeout_sec=1200)
        rc = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    elif not yosys:
        log = "Yosys executable was not found in the active ChipLoop tool profile.\n"
    elif not rtl_files:
        log = "Missing RTL inputs for post-tapeout LEC.\n"
    elif not staged_netlist:
        log = "Missing final tapeout implementation netlist for post-tapeout LEC.\n"
    elif not yosys_stdcell_verilog:
        log = "Missing standard-cell functional models for post-tapeout LEC.\n"
    elif missing_stdcell_models:
        log = "Standard-cell model coverage is incomplete for post-tapeout LEC.\nMissing cells:\n" + "\n".join(missing_stdcell_models) + "\n"
    _write_text(log_path, log)

    verdict, unproven = _classify(rc, log, bool(yosys))
    if not rtl_files or not staged_netlist:
        verdict = "incomplete_inputs"
    if not yosys_stdcell_verilog:
        verdict = "missing_stdcell_models"
    if missing_stdcell_models:
        verdict = "incomplete_stdcell_models"
    failure_reason = _failure_reason(
        verdict,
        rtl_files=rtl_files,
        netlist=staged_netlist,
        yosys=yosys,
        liberty_files=liberty_files,
        stdcell_verilog=yosys_stdcell_verilog,
        missing_cells=missing_stdcell_models,
        unproven=unproven,
    )

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": verdict,
        "tool": "yosys",
        "tool_available": bool(yosys),
        "return_code": rc,
        "top_module": top,
        "comparison": "rtl_vs_tapeout_netlist",
        "rtl_files": [os.path.basename(path) for path in rtl_files],
        "yosys_rtl_files": [os.path.basename(path) for path in prepared_rtl_files],
        "golden_macro_blackbox_stubs": [os.path.basename(path) for path in golden_macro_stubs],
        "tapeout_netlist": os.path.basename(staged_netlist) if staged_netlist else None,
        "source_tapeout_netlist": final_netlist,
        "liberty_files": [os.path.basename(path) for path in liberty_files],
        "yosys_stdcell_verilog_files": [os.path.basename(path) for path in yosys_stdcell_verilog],
        "generated_stdcell_model": os.path.basename(generated_stdcell_model) if generated_stdcell_model else None,
        "ignored_gate_ports": ignored_gate_ports,
        "missing_stdcell_models": missing_stdcell_models,
        "missing_stdcell_model_count": len(missing_stdcell_models),
        "unproven_points": unproven,
        "failure_reason": failure_reason,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "script": "digital/tapeout_lec/yosys_tapeout_lec.ys",
            "log": "digital/tapeout_lec/logs/yosys_tapeout_lec.log",
            "summary": "digital/tapeout_lec/tapeout_lec_summary.json",
            "report": "digital/tapeout_lec/tapeout_lec_report.md",
            "tapeout_netlist": f"digital/tapeout_lec/input/{os.path.basename(staged_netlist)}" if staged_netlist else None,
            "generated_stdcell_model": "digital/tapeout_lec/input/stdcell_functional_wrappers.v" if generated_stdcell_model else None,
            "golden_macro_blackbox_stubs": [f"digital/tapeout_lec/input/{os.path.basename(path)}" for path in golden_macro_stubs],
        },
    }
    report = "\n".join([
        "# Tapeout Logic Equivalence Check",
        "",
        f"- Status: `{verdict}`",
        "- Comparison: `RTL vs final tapeout implementation netlist`",
        f"- Top module: `{top}`",
        f"- RTL files: `{len(rtl_files)}`",
        f"- Tapeout netlist: `{os.path.basename(staged_netlist) if staged_netlist else 'missing'}`",
        f"- Ignored physical-only gate ports: `{', '.join(ignored_gate_ports) if ignored_gate_ports else 'none'}`",
        f"- Standard-cell models loaded: `{len(yosys_stdcell_verilog)}`",
        f"- Missing standard-cell models: `{len(missing_stdcell_models)}`",
        f"- Unproven points: `{unproven}`",
        f"- Return code: `{rc}`",
        f"- Failure reason: `{failure_reason}`",
        "",
        "This is distinct from synthesis LEC. Synthesis LEC compares RTL against `digital/synth/netlist/*_synth.v`; tapeout LEC compares RTL against the final implementation netlist collected after physical implementation.",
    ]) + "\n"

    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    if staged_netlist:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"tapeout_lec/input/{os.path.basename(staged_netlist)}", open(staged_netlist, "r", encoding="utf-8", errors="ignore").read())
    if generated_stdcell_model:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout_lec/input/stdcell_functional_wrappers.v", open(generated_stdcell_model, "r", encoding="utf-8", errors="ignore").read())
    for stub_path in golden_macro_stubs:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"tapeout_lec/input/{os.path.basename(stub_path)}", open(stub_path, "r", encoding="utf-8", errors="ignore").read())
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout_lec/yosys_tapeout_lec.ys", script)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout_lec/logs/yosys_tapeout_lec.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout_lec/tapeout_lec_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "tapeout_lec/tapeout_lec_report.md", report)

    state.setdefault("digital", {})["tapeout_lec"] = {
        "status": verdict,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
        "netlist": staged_netlist,
    }
    state["status"] = f"{AGENT_NAME}: {verdict}"
    return state
