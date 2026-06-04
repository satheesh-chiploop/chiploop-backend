import glob
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Scan ATPG Coverage Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _existing_path(value: Any, workflow_dir: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if workflow_dir and not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            cand = os.path.abspath(cand)
            if os.path.exists(cand):
                return cand
        except (TypeError, ValueError, OSError):
            continue
    return None


def _scan_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    dft = digital.get("dft") if isinstance(digital.get("dft"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (
        dft.get("scan_stitched_netlist"),
        state.get("scan_stitched_netlist"),
        synth.get("netlist"),
        digital.get("synth_netlist"),
        state.get("synth_netlist"),
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    patterns = [
        os.path.join(workflow_dir, "digital", "dft", "scan_stitched_netlist.v"),
        os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v"),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return os.path.abspath(hits[0])
    return None


def _tool_choice(state: dict) -> tuple[str | None, str | None]:
    for name in ("fault", "atalanta", "podem"):
        path = tool_path(name, state)
        if path:
            return name, path
    return None, None


def _is_wrong_fault_tool(log: str) -> bool:
    text = log.lower()
    return (
        "network resilience" in text
        or "resilience proxy" in text
        or "injecting various faults" in text
        or "fault injection" in text and "atpg" not in text
    )


def _run_detected_tool(state: dict, tool_name: str, stage_dir: str, netlist_path: str) -> tuple[int | None, str]:
    # Open-source ATPG tools do not share a stable CLI. For production use,
    # private/hybrid profiles should set CHIPLOOP_ATPG_COMMAND or map a tool
    # adapter. By default we collect tool readiness/version/help without
    # inventing fake coverage.
    configured = os.getenv("CHIPLOOP_ATPG_COMMAND", "").strip()
    if configured:
        script_path = os.path.join(stage_dir, "run_configured_atpg.sh")
        _write_text(script_path, f"#!/usr/bin/env bash\nset -euo pipefail\n{configured}\n")
        proc = run_command(state, "scan_atpg", ["bash", script_path], cwd=stage_dir, timeout_sec=900)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    help_arg = "--help"
    if tool_name in {"atalanta", "podem"}:
        help_arg = "-h"
    proc = run_command(state, "scan_atpg_probe", [tool_name, help_arg], cwd=stage_dir, timeout_sec=120)
    log = (proc.stdout or "") + (proc.stderr or "")
    log += "\n\nChipLoop did not run ATPG patterns because CHIPLOOP_ATPG_COMMAND is not configured for this tool.\n"
    log += f"Input scan/gate netlist staged at: {netlist_path}\n"
    return proc.returncode, log


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "atpg")
    logs_dir = os.path.join(stage_dir, "logs")
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(logs_dir)
    _ensure_dir(input_dir)

    netlist = _scan_netlist(state, workflow_dir)
    input_netlist = os.path.join(input_dir, "scan_or_gate_netlist.v")
    if netlist:
        _write_text(input_netlist, _read_text(netlist))

    tool_name, tool_exe = _tool_choice(state)
    rc = None
    log = ""
    if not netlist:
        status = "incomplete_inputs"
        log = "No scan-stitched or synthesized gate netlist found for ATPG.\n"
    elif not tool_name:
        status = "tool_unavailable"
        log = "No open-source ATPG tool found. Configure CHIPLOOP_FAULT, CHIPLOOP_ATALANTA, CHIPLOOP_PODEM, or CHIPLOOP_ATPG_COMMAND.\n"
    else:
        rc, log = _run_detected_tool(state, tool_name, stage_dir, input_netlist)
        configured = bool(os.getenv("CHIPLOOP_ATPG_COMMAND", "").strip())
        if tool_name == "fault" and not configured and _is_wrong_fault_tool(log):
            status = "wrong_tool_detected"
            log += "\n\nThe detected `fault` executable is not a digital ATPG tool; it appears to be a network resilience/fault-injection CLI. Ignoring it for ATPG coverage.\n"
        else:
            status = "adapter_ready" if configured else "tool_detected_needs_adapter_command"
        if status != "wrong_tool_detected" and rc not in (0, None) and "usage" not in log.lower() and "help" not in log.lower():
            status = "tool_probe_failed"

    log_path = os.path.join(logs_dir, "scan_atpg.log")
    summary_path = os.path.join(stage_dir, "atpg_summary.json")
    report_path = os.path.join(stage_dir, "atpg_report.md")
    _write_text(log_path, log)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": status,
        "tool": tool_name,
        "tool_executable": tool_exe,
        "return_code": rc,
        "input_netlist": os.path.basename(netlist) if netlist else None,
        "pattern_count": None,
        "stuck_at_coverage_pct": None,
        "faults_detected": None,
        "faults_undetected": None,
        "faults_aborted": None,
        "coverage_source": "not_reported" if status != "adapter_ready" else "configured_adapter",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "summary": "digital/atpg/atpg_summary.json",
            "report": "digital/atpg/atpg_report.md",
            "log": "digital/atpg/logs/scan_atpg.log",
            "input_netlist": "digital/atpg/input/scan_or_gate_netlist.v" if netlist else None,
        },
    }
    report = "\n".join([
        "# Scan ATPG Coverage",
        "",
        f"- Status: `{status}`",
        f"- Tool: `{tool_name or 'not found'}`",
        f"- Input netlist: `{os.path.basename(netlist) if netlist else 'missing'}`",
        "- Pattern count: `not reported`",
        "- Stuck-at coverage: `not reported`",
        "",
        "This agent is adapter-ready for open-source ATPG tools. Set `CHIPLOOP_ATPG_COMMAND` to the validated command for the installed toolchain to produce real pattern and coverage reports.",
        "If status is `wrong_tool_detected`, the executable name matched but the binary is not a digital ATPG tool.",
    ]) + "\n"
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    if netlist:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/input/scan_or_gate_netlist.v", _read_text(input_netlist))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/logs/scan_atpg.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_report.md", report)

    digital = state.setdefault("digital", {})
    digital["atpg"] = {
        "status": status,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
    }
    state["status"] = f"{AGENT_NAME}: {status}"
    return state
