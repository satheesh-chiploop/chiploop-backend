import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.profiles import get_tool_profile
from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital UPF Static Check Agent"


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


def _find_upf(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    signoff = state.get("signoff") if isinstance(state.get("signoff"), dict) else {}
    for cand in (
        state.get("power_intent_upf"),
        state.get("upf_path"),
        digital.get("power_intent_upf") if isinstance(digital, dict) else None,
        signoff.get("power_intent_upf") if isinstance(signoff, dict) else None,
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    patterns = [
        os.path.join(workflow_dir, "digital", "*.upf"),
        os.path.join(workflow_dir, "signoff", "*.upf"),
        os.path.join(workflow_dir, "power", "*.upf"),
        os.path.join(workflow_dir, "**", "*.upf"),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return os.path.abspath(hits[0])
    return None


def _find_rtl_files(state: dict, workflow_dir: str) -> list[str]:
    candidates: list[str] = []
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    rtl_files = digital.get("rtl_files") if isinstance(digital.get("rtl_files"), list) else []
    candidates.extend(str(x) for x in rtl_files if x)
    if state.get("rtl_path"):
        candidates.append(str(state["rtl_path"]))
    for pattern in (
        os.path.join(workflow_dir, "rtl", "*.sv"),
        os.path.join(workflow_dir, "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "dqa", "handoff", "rtl", "*.sv"),
        os.path.join(workflow_dir, "digital", "dqa", "handoff", "rtl", "*.v"),
        os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v"),
    ):
        candidates.extend(glob.glob(pattern))

    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        path = _existing_path(cand, workflow_dir)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _parse_upf(text: str) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    unsupported: list[str] = []
    domains: list[dict[str, Any]] = []
    supply_ports: set[str] = set()
    supply_nets: set[str] = set()
    domain_supply_nets: list[str] = []
    isolation: list[str] = []
    retention: list[str] = []
    level_shifters: list[str] = []
    power_states: list[str] = []
    pst: list[str] = []

    supported_prefixes = {
        "create_supply_port",
        "create_supply_net",
        "connect_supply_net",
        "create_power_domain",
        "set_domain_supply_net",
        "set_isolation",
        "set_isolation_control",
        "set_retention",
        "set_retention_control",
        "set_level_shifter",
        "add_power_state",
        "create_pst",
        "add_pst_state",
    }

    logical_lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1].strip() + " "
            continue
        line = current + line
        current = ""
        logical_lines.append(line.strip())
    if current.strip():
        logical_lines.append(current.strip())

    for line in logical_lines:
        cmd = line.split()[0] if line.split() else ""
        commands.append({"command": cmd, "line": line})
        if cmd not in supported_prefixes:
            unsupported.append(line)
        if cmd == "create_supply_port":
            parts = line.split()
            if len(parts) > 1:
                supply_ports.add(parts[1])
        elif cmd == "create_supply_net":
            parts = line.split()
            if len(parts) > 1:
                supply_nets.add(parts[1])
        elif cmd == "create_power_domain":
            name = line.split()[1] if len(line.split()) > 1 else "UNKNOWN"
            elements_match = re.search(r"-elements\s+\{([^}]*)\}", line)
            elements = elements_match.group(1).split() if elements_match else []
            domains.append({"name": name, "elements": elements, "line": line})
        elif cmd == "set_domain_supply_net":
            domain_supply_nets.append(line)
        elif cmd.startswith("set_isolation"):
            isolation.append(line)
        elif cmd.startswith("set_retention"):
            retention.append(line)
        elif cmd == "set_level_shifter":
            level_shifters.append(line)
        elif cmd == "add_power_state":
            power_states.append(line)
        elif cmd in {"create_pst", "add_pst_state"}:
            pst.append(line)

    return {
        "commands": commands,
        "unsupported_commands": unsupported,
        "domains": domains,
        "supply_ports": sorted(supply_ports),
        "supply_nets": sorted(supply_nets),
        "domain_supply_nets": domain_supply_nets,
        "isolation": isolation,
        "retention": retention,
        "level_shifters": level_shifters,
        "power_states": power_states,
        "pst": pst,
    }


def _rtl_modules_and_instances(rtl_files: list[str]) -> tuple[set[str], set[str]]:
    modules: set[str] = set()
    instances: set[str] = set()
    for path in rtl_files:
        text = _read_text(path)
        for m in re.finditer(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE):
            modules.add(m.group(1))
        for m in re.finditer(
            r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\([^;]*?\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
            text,
            flags=re.MULTILINE | re.DOTALL,
        ):
            cell, inst = m.group(1), m.group(2)
            if cell not in {"module", "if", "for", "while", "always", "assign"}:
                instances.add(inst)
    return modules, instances


def _intent_expectations(state: dict) -> dict[str, bool]:
    signoff = state.get("signoff") if isinstance(state.get("signoff"), dict) else {}
    pi = signoff.get("power_intent") if isinstance(signoff.get("power_intent"), dict) else {}
    spec = state.get("digital_spec") if isinstance(state.get("digital_spec"), dict) else {}
    intent = spec.get("power_intent") if isinstance(spec.get("power_intent"), dict) else {}
    isolation_requested = bool(pi.get("isolation") or intent.get("isolation"))
    retention_requested = bool(pi.get("retention") or intent.get("retention"))
    level_shifter_requested = bool(intent.get("level_shifters") or intent.get("level_shifter"))
    return {
        "isolation_requested": isolation_requested,
        "retention_requested": retention_requested,
        "level_shifter_requested": level_shifter_requested,
    }


def _run_openroad_read_upf(state: dict, stage_dir: str, upf_path: str | None) -> dict[str, Any]:
    openroad = tool_path("openroad", state)
    if not upf_path:
        return {"status": "not_run", "reason": "missing_upf", "tool": "openroad"}
    if not openroad:
        return {"status": "tool_unavailable", "tool": "openroad"}

    tcl_path = os.path.join(stage_dir, "openroad_read_upf.tcl")
    local_upf = os.path.join(stage_dir, "input_power_intent.upf")
    _write_text(local_upf, _read_text(upf_path))
    _write_text(tcl_path, 'read_upf "input_power_intent.upf"\nexit\n')
    proc = run_command(state, "upf_static_openroad_probe", ["openroad", "-exit", "openroad_read_upf.tcl"], cwd=stage_dir, timeout_sec=120)
    log = (proc.stdout or "") + (proc.stderr or "")
    _write_text(os.path.join(stage_dir, "logs", "openroad_read_upf.log"), log)
    if proc.returncode == 0:
        status = "pass"
    elif "unknown command" in log.lower() or "invalid command" in log.lower():
        status = "unsupported_by_tool"
    else:
        status = "fail"
    return {
        "status": status,
        "tool": "openroad",
        "return_code": proc.returncode,
        "log": "digital/upf/logs/openroad_read_upf.log",
    }


def _run_private_adapter(state: dict, stage_dir: str, upf_path: str | None) -> dict[str, Any]:
    profile = get_tool_profile(state)
    commands = profile.get("commands") if isinstance(profile.get("commands"), dict) else {}
    profile_command = commands.get("upf_static_check")
    if isinstance(profile_command, dict):
        profile_command = profile_command.get("command")
    configured = str(profile_command or os.getenv("CHIPLOOP_UPF_STATIC_CHECK_COMMAND", "")).strip()
    if not configured:
        return {"status": "not_configured"}
    if not upf_path:
        return {"status": "not_run", "reason": "missing_upf"}
    script_path = os.path.join(stage_dir, "run_private_upf_static_check.sh")
    _write_text(script_path, f"#!/usr/bin/env bash\nset -euo pipefail\n{configured}\n")
    proc = run_command(
        state,
        "upf_static_private_adapter",
        ["bash", script_path],
        cwd=stage_dir,
        timeout_sec=int(os.getenv("CHIPLOOP_UPF_STATIC_CHECK_TIMEOUT_SEC", "900")),
        env={"CHIPLOOP_UPF_FILE": upf_path, "CHIPLOOP_UPF_STAGE_DIR": stage_dir},
    )
    log = (proc.stdout or "") + (proc.stderr or "")
    _write_text(os.path.join(stage_dir, "logs", "private_upf_static_check.log"), log)
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "return_code": proc.returncode,
        "log": "digital/upf/logs/private_upf_static_check.log",
        "command_configured": True,
    }


def _verdict(checks: list[dict[str, Any]]) -> str:
    severities = [str(c.get("severity", "info")) for c in checks]
    if "error" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "pass"


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "upf")
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(logs_dir)

    upf_path = _find_upf(state, workflow_dir)
    rtl_files = _find_rtl_files(state, workflow_dir)
    upf_text = _read_text(upf_path)
    parsed = _parse_upf(upf_text) if upf_text else {
        "commands": [],
        "unsupported_commands": [],
        "domains": [],
        "supply_ports": [],
        "supply_nets": [],
        "domain_supply_nets": [],
        "isolation": [],
        "retention": [],
        "level_shifters": [],
        "power_states": [],
        "pst": [],
    }
    modules, instances = _rtl_modules_and_instances(rtl_files)
    expectations = _intent_expectations(state)

    checks: list[dict[str, Any]] = []
    if not upf_path:
        checks.append({"name": "upf_present", "status": "fail", "severity": "error", "message": "No UPF artifact found."})
    else:
        checks.append({"name": "upf_present", "status": "pass", "severity": "info", "message": os.path.basename(upf_path)})
    if parsed["domains"]:
        checks.append({"name": "power_domains", "status": "pass", "severity": "info", "count": len(parsed["domains"])})
    else:
        checks.append({"name": "power_domains", "status": "fail", "severity": "error", "message": "No create_power_domain command found."})
    if parsed["supply_ports"] and parsed["supply_nets"]:
        checks.append({"name": "supplies", "status": "pass", "severity": "info", "ports": parsed["supply_ports"], "nets": parsed["supply_nets"]})
    else:
        checks.append({"name": "supplies", "status": "fail", "severity": "error", "message": "Missing supply ports or supply nets."})
    if len(parsed["domain_supply_nets"]) >= len(parsed["domains"]):
        checks.append({"name": "domain_supply_mapping", "status": "pass", "severity": "info"})
    else:
        checks.append({"name": "domain_supply_mapping", "status": "fail", "severity": "error", "message": "Not every domain has a set_domain_supply_net mapping."})

    unresolved_elements: list[str] = []
    for domain in parsed["domains"]:
        for element in domain.get("elements") or []:
            clean = str(element).strip("{}")
            if clean and clean not in modules and clean not in instances:
                unresolved_elements.append(clean)
    if unresolved_elements and rtl_files:
        checks.append({"name": "domain_elements_resolve", "status": "warn", "severity": "warn", "unresolved": sorted(set(unresolved_elements))})
    elif rtl_files:
        checks.append({"name": "domain_elements_resolve", "status": "pass", "severity": "info"})
    else:
        checks.append({"name": "domain_elements_resolve", "status": "not_run", "severity": "info", "message": "No RTL files available for element resolution."})

    if parsed["unsupported_commands"]:
        checks.append({"name": "unsupported_commands", "status": "warn", "severity": "warn", "count": len(parsed["unsupported_commands"])})
    else:
        checks.append({"name": "unsupported_commands", "status": "pass", "severity": "info"})
    checks.append({
        "name": "isolation_intent",
        "status": "pass" if parsed["isolation"] or not expectations["isolation_requested"] else "warn",
        "severity": "info" if parsed["isolation"] or not expectations["isolation_requested"] else "warn",
        "count": len(parsed["isolation"]),
    })
    checks.append({
        "name": "retention_intent",
        "status": "pass" if parsed["retention"] or not expectations["retention_requested"] else "warn",
        "severity": "info" if parsed["retention"] or not expectations["retention_requested"] else "warn",
        "count": len(parsed["retention"]),
    })
    checks.append({
        "name": "level_shifter_intent",
        "status": "pass" if parsed["level_shifters"] or not expectations["level_shifter_requested"] else "warn",
        "severity": "info" if parsed["level_shifters"] or not expectations["level_shifter_requested"] else "warn",
        "count": len(parsed["level_shifters"]),
    })
    checks.append({
        "name": "power_state_table",
        "status": "pass" if parsed["pst"] or parsed["power_states"] else "warn",
        "severity": "info" if parsed["pst"] or parsed["power_states"] else "warn",
        "message": "No PST/power states found." if not (parsed["pst"] or parsed["power_states"]) else "",
    })

    openroad_result = _run_openroad_read_upf(state, stage_dir, upf_path)
    private_result = _run_private_adapter(state, stage_dir, upf_path)
    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": _verdict(checks),
        "upf_file": os.path.basename(upf_path) if upf_path else None,
        "domain_count": len(parsed["domains"]),
        "supply_port_count": len(parsed["supply_ports"]),
        "supply_net_count": len(parsed["supply_nets"]),
        "isolation_rule_count": len(parsed["isolation"]),
        "retention_rule_count": len(parsed["retention"]),
        "level_shifter_rule_count": len(parsed["level_shifters"]),
        "pst_present": bool(parsed["pst"] or parsed["power_states"]),
        "unsupported_command_count": len(parsed["unsupported_commands"]),
        "openroad_read_upf": openroad_result,
        "private_adapter": private_result,
        "power_aware_sim": {
            "status": "not_supported_by_open_source_flow",
            "note": "This static checker does not simulate UPF supply corruption/isolation/retention semantics.",
        },
        "checks": checks,
        "unsupported_commands": parsed["unsupported_commands"],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "summary": "digital/upf/upf_static_check.json",
            "report": "digital/upf/upf_static_check.md",
            "unsupported": "digital/upf/upf_unsupported_commands.txt",
        },
    }
    report_lines = [
        "# UPF Static Check",
        "",
        f"- Status: `{summary['status']}`",
        f"- UPF file: `{summary['upf_file'] or 'missing'}`",
        f"- Power domains: `{summary['domain_count']}`",
        f"- Supply ports/nets: `{summary['supply_port_count']}/{summary['supply_net_count']}`",
        f"- Isolation rules: `{summary['isolation_rule_count']}`",
        f"- Retention rules: `{summary['retention_rule_count']}`",
        f"- Level shifter rules: `{summary['level_shifter_rule_count']}`",
        f"- PST/power states: `{'present' if summary['pst_present'] else 'missing'}`",
        f"- OpenROAD read_upf: `{openroad_result.get('status')}`",
        f"- Private adapter: `{private_result.get('status')}`",
        "",
        "This is an open-source-compatible structural UPF check. It is not a replacement for commercial power-aware simulation or signoff.",
        "",
        "## Checks",
    ]
    for check in checks:
        report_lines.append(f"- `{check.get('name')}`: `{check.get('status')}` {check.get('message') or ''}".rstrip())
    report = "\n".join(report_lines) + "\n"
    unsupported_text = "\n".join(parsed["unsupported_commands"]) + ("\n" if parsed["unsupported_commands"] else "")

    summary_path = os.path.join(stage_dir, "upf_static_check.json")
    report_path = os.path.join(stage_dir, "upf_static_check.md")
    unsupported_path = os.path.join(stage_dir, "upf_unsupported_commands.txt")
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)
    _write_text(unsupported_path, unsupported_text or "No unsupported commands detected by ChipLoop UPF static checker.\n")

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "upf/upf_static_check.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "upf/upf_static_check.md", report)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "upf/upf_unsupported_commands.txt", _read_text(unsupported_path))
    openroad_log = os.path.join(stage_dir, "logs", "openroad_read_upf.log")
    if os.path.exists(openroad_log):
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "upf/logs/openroad_read_upf.log", _read_text(openroad_log))
    private_log = os.path.join(stage_dir, "logs", "private_upf_static_check.log")
    if os.path.exists(private_log):
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "upf/logs/private_upf_static_check.log", _read_text(private_log))

    digital = state.setdefault("digital", {})
    digital["upf_static_check"] = {
        "status": summary["status"],
        "summary_json": summary_path,
        "report_md": report_path,
        "openroad_read_upf": openroad_result,
        "private_adapter": private_result,
    }
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
