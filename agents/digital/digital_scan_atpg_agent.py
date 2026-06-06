import glob
import json
import os
import re
import shlex
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


def _read_json(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _number_or_none(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value.strip().rstrip("%"))
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


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


def _bench_name(signal: str) -> str:
    text = str(signal or "").strip()
    if text.startswith("\\"):
        text = text[1:].strip()
    text = text.replace("[", "_").replace("]", "")
    text = re.sub(r"[^A-Za-z0-9_]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "unnamed"
    if not text[0].isalpha():
        text = "n_" + text
    return text


def _expand_signal_decl(width: str | None, names: str) -> list[str]:
    raw_names = [name.strip() for name in names.split(",") if name.strip()]
    if not width:
        return raw_names
    match = re.search(r"\[(\d+)\s*:\s*(\d+)\]", width)
    if not match:
        return raw_names
    left = int(match.group(1))
    right = int(match.group(2))
    step = 1 if right >= left else -1
    indexes = range(left, right + step, step)
    return [f"{name}[{idx}]" for name in raw_names for idx in indexes]


def _declared_ports(netlist_text: str, direction: str) -> list[str]:
    ports: list[str] = []
    pattern = re.compile(rf"^\s*{direction}\s*(?P<width>\[[^\]]+\])?\s*(?P<names>[^;]+);", flags=re.MULTILINE)
    for match in pattern.finditer(netlist_text):
        ports.extend(_expand_signal_decl(match.group("width"), match.group("names")))
    return ports


def _parse_instances(netlist_text: str) -> list[tuple[str, str, dict[str, str]]]:
    instances: list[tuple[str, str, dict[str, str]]] = []
    pattern = re.compile(
        r"(?P<cell>sky130_fd_sc_hd__[A-Za-z0-9_]+)\s+"
        r"(?P<inst>(?:\\[^\s(]+|[A-Za-z_][A-Za-z0-9_$]*))\s*"
        r"\((?P<ports>.*?)\);",
        flags=re.DOTALL,
    )
    for match in pattern.finditer(netlist_text):
        ports = {
            item.group("pin"): item.group("signal").strip()
            for item in re.finditer(r"\.(?P<pin>[A-Za-z0-9_]+)\s*\(\s*(?P<signal>.*?)\s*\)", match.group("ports"), flags=re.DOTALL)
        }
        instances.append((match.group("cell"), match.group("inst"), ports))
    return instances


def _out_signal(ports: dict[str, str]) -> str | None:
    for pin in ("X", "Y", "Q"):
        if pin in ports:
            return ports[pin]
    return None


def _logic_inputs(ports: dict[str, str], skip: set[str] | None = None) -> list[tuple[str, str]]:
    skip = skip or set()
    return [(pin, signal) for pin, signal in ports.items() if pin not in skip and pin not in {"X", "Y", "Q", "Q_N"}]


def _bench_gate(lines: list[str], output: str, gate: str, inputs: list[str]) -> None:
    lines.append(f"{_bench_name(output)} = {gate}({', '.join(_bench_name(item) for item in inputs)})")


def _bench_gate_signal_sets(gates: list[str]) -> tuple[set[str], set[str]]:
    driven: set[str] = set()
    used: set[str] = set()
    for line in gates:
        match = re.match(r"\s*(?P<lhs>[A-Za-z0-9_]+)\s*=\s*[A-Z]+\((?P<rhs>.*?)\)\s*$", line)
        if not match:
            continue
        driven.add(match.group("lhs"))
        used.update(item.strip() for item in match.group("rhs").split(",") if item.strip())
    return driven, used


def _maybe_inverted(lines: list[str], signal: str, pin: str, inst: str) -> str:
    if not pin.endswith("_N"):
        return signal
    temp = f"{inst}_{pin}_inv"
    _bench_gate(lines, temp, "NOT", [signal])
    return temp


def _emit_basic_gate(lines: list[str], cell_base: str, inst: str, ports: dict[str, str]) -> bool:
    out = _out_signal(ports)
    if not out:
        return False
    if cell_base.startswith("inv"):
        _bench_gate(lines, out, "NOT", [ports.get("A", "")])
        return True
    if cell_base.startswith(("buf", "clkbuf", "dly")):
        _bench_gate(lines, out, "BUFF", [ports.get("A", "")])
        return True
    gate_map = [
        ("nand", "NAND"),
        ("nor", "NOR"),
        ("and", "AND"),
        ("or", "OR"),
    ]
    for prefix, gate in gate_map:
        if cell_base.startswith(prefix):
            inputs = [_maybe_inverted(lines, signal, pin, inst) for pin, signal in _logic_inputs(ports)]
            if not inputs:
                return False
            _bench_gate(lines, out, gate, inputs)
            return True
    return False


def _emit_mux2(lines: list[str], inst: str, ports: dict[str, str]) -> bool:
    out = _out_signal(ports)
    a0, a1, sel = ports.get("A0"), ports.get("A1"), ports.get("S")
    if not out or not a0 or not a1 or not sel:
        return False
    nsel = f"{inst}_S_inv"
    t0 = f"{inst}_mux_a0"
    t1 = f"{inst}_mux_a1"
    _bench_gate(lines, nsel, "NOT", [sel])
    _bench_gate(lines, t0, "AND", [a0, nsel])
    _bench_gate(lines, t1, "AND", [a1, sel])
    _bench_gate(lines, out, "OR", [t0, t1])
    return True


def _emit_xor2(lines: list[str], cell_base: str, inst: str, ports: dict[str, str]) -> bool:
    out = _out_signal(ports)
    a = ports.get("A")
    b = ports.get("B")
    if not out or not a or not b:
        return False
    na = f"{inst}_A_inv"
    nb = f"{inst}_B_inv"
    t0 = f"{inst}_xor_t0"
    t1 = f"{inst}_xor_t1"
    xor_out = f"{inst}_xor"
    _bench_gate(lines, na, "NOT", [a])
    _bench_gate(lines, nb, "NOT", [b])
    _bench_gate(lines, t0, "AND", [a, nb])
    _bench_gate(lines, t1, "AND", [na, b])
    _bench_gate(lines, xor_out, "OR", [t0, t1])
    if cell_base.startswith("xnor"):
        _bench_gate(lines, out, "NOT", [xor_out])
    else:
        _bench_gate(lines, out, "BUFF", [xor_out])
    return True


def _emit_compound(lines: list[str], cell_base: str, inst: str, ports: dict[str, str]) -> bool:
    out = _out_signal(ports)
    if not out:
        return False
    inverted = cell_base.endswith("i")
    base = cell_base[:-1] if inverted else cell_base
    final_gate: str
    group_letters = "ABCDE"
    if base.startswith("a"):
        final_gate = "OR"
        spec = "".join(ch for ch in base[1:].rstrip("o") if ch.isdigit())
    elif base.startswith("o"):
        final_gate = "AND"
        spec = "".join(ch for ch in base[1:].rstrip("a") if ch.isdigit())
    else:
        return False
    if not spec:
        return False
    groups = [
        [group_letters[group_idx] + str(pin_idx) for pin_idx in range(1, int(count) + 1)]
        for group_idx, count in enumerate(spec[:len(group_letters)])
    ]

    terms: list[str] = []
    group_gate = "AND" if final_gate == "OR" else "OR"
    for idx, pins in enumerate(groups):
        signals = []
        for pin in pins:
            signal = ports.get(pin) or ports.get(pin + "_N")
            if not signal:
                continue
            signals.append(_maybe_inverted(lines, signal, pin + "_N" if pin + "_N" in ports else pin, inst))
        if not signals:
            continue
        if len(signals) == 1:
            terms.append(signals[0])
        else:
            temp = f"{inst}_term_{idx}"
            _bench_gate(lines, temp, group_gate, signals)
            terms.append(temp)
    if not terms:
        return False
    final_out = f"{inst}_compound" if inverted else out
    _bench_gate(lines, final_out, final_gate, terms)
    if inverted:
        _bench_gate(lines, out, "NOT", [final_out])
    return True


def _generate_full_scan_bench(netlist_text: str) -> tuple[str, dict]:
    primary_inputs = [_bench_name(sig) for sig in _declared_ports(netlist_text, "input")]
    primary_outputs = [_bench_name(sig) for sig in _declared_ports(netlist_text, "output")]
    lines: list[str] = ["# Auto-generated full-scan combinational model for Atalanta"]
    gates: list[str] = []
    unsupported: list[str] = []
    scan_q_inputs: list[str] = []
    scan_d_outputs: list[str] = []

    for cell, inst, ports in _parse_instances(netlist_text):
        cell_base = re.sub(r"_\d+$", "", cell.replace("sky130_fd_sc_hd__", ""))
        if cell_base.startswith("sdfrtp"):
            q = ports.get("Q")
            d = ports.get("D")
            if q:
                scan_q_inputs.append(_bench_name(q))
            if d:
                scan_d_outputs.append(_bench_name(d))
            continue
        if cell_base.startswith(("xor2", "xnor2")):
            ok = _emit_xor2(gates, cell_base, inst, ports)
        elif cell_base.startswith("mux2"):
            ok = _emit_mux2(gates, inst, ports)
        elif cell_base[0:1] in {"a", "o"} and _emit_compound(gates, cell_base, inst, ports):
            ok = True
        else:
            ok = _emit_basic_gate(gates, cell_base, inst, ports)
        if not ok:
            unsupported.append(cell)

    for match in re.finditer(r"assign\s+(?P<lhs>.*?)\s*=\s*(?P<rhs>.*?)\s*;", netlist_text):
        _bench_gate(gates, match.group("lhs"), "BUFF", [match.group("rhs")])

    driven, used = _bench_gate_signal_sets(gates)
    candidate_inputs = set(primary_inputs + scan_q_inputs)
    inputs = sorted(candidate_inputs & used)
    outputs = sorted((set(primary_outputs) & (driven | candidate_inputs)) | (set(scan_d_outputs) & driven))
    for name in inputs:
        lines.append(f"INPUT({name})")
    for name in outputs:
        lines.append(f"OUTPUT({name})")
    lines.extend(gates)
    meta = {
        "status": "generated" if not unsupported and gates and inputs and outputs else "failed",
        "inputs": len(inputs),
        "outputs": len(outputs),
        "gates": len(gates),
        "scan_state_inputs": len(set(scan_q_inputs)),
        "scan_capture_outputs": len(set(scan_d_outputs)),
        "unsupported_cells": sorted(set(unsupported)),
    }
    return "\n".join(lines) + "\n", meta


def _scan_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    dft = digital.get("dft") if isinstance(digital.get("dft"), dict) else {}
    for cand in (
        dft.get("scan_stitched_netlist"),
        state.get("scan_stitched_netlist"),
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    patterns = [
        os.path.join(workflow_dir, "digital", "dft", "scan_stitched_netlist.v"),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return os.path.abspath(hits[0])
    return None


def _tool_choice(state: dict, stage_dir: str) -> tuple[str | None, str | None, list[dict[str, str]]]:
    diagnostics: list[dict[str, str]] = []
    for name in ("atalanta", "podem", "fault"):
        path = tool_path(name, state)
        if not path:
            continue
        if name == "fault":
            proc = run_command(state, "scan_atpg_probe", ["fault", "--help"], cwd=stage_dir, timeout_sec=120)
            log = (proc.stdout or "") + (proc.stderr or "")
            if _is_wrong_fault_tool(log):
                diagnostics.append({
                    "tool": name,
                    "executable": path,
                    "status": "skipped_wrong_tool",
                })
                continue
        return name, path, diagnostics
    return None, None, diagnostics


def _is_wrong_fault_tool(log: str) -> bool:
    text = log.lower()
    return (
        "network resilience" in text
        or "resilience proxy" in text
        or "injecting various faults" in text
        or "fault injection" in text and "atpg" not in text
    )


def _configured_command_missing(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return "CHIPLOOP_ATPG_COMMAND is not a valid shell command"
    if not parts:
        return "CHIPLOOP_ATPG_COMMAND is empty"
    head = parts[0]
    if (head.startswith("/") or head.startswith("./") or head.startswith("../")) and not os.path.exists(head):
        return f"configured ATPG adapter does not exist: {head}"
    return None


def _run_detected_tool(state: dict, tool_name: str, stage_dir: str, netlist_path: str, metrics_path: str, bench_path: str | None) -> tuple[int | None, str]:
    # Open-source ATPG tools do not share a stable CLI. For production use,
    # private/hybrid profiles should set CHIPLOOP_ATPG_COMMAND or map a tool
    # adapter. By default we collect tool readiness/version/help without
    # inventing fake coverage.
    configured = os.getenv("CHIPLOOP_ATPG_COMMAND", "").strip()
    if configured:
        missing_reason = _configured_command_missing(configured)
        if missing_reason:
            return 127, missing_reason + "\n"
        script_path = os.path.join(stage_dir, "run_configured_atpg.sh")
        patterns_path = os.path.join(stage_dir, "patterns")
        _ensure_dir(patterns_path)
        _write_text(
            script_path,
            "\n".join([
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"export CHIPLOOP_ATPG_STAGE_DIR={json.dumps(stage_dir)}",
                f"export CHIPLOOP_ATPG_INPUT_NETLIST={json.dumps(netlist_path)}",
                f"export CHIPLOOP_ATPG_BENCH_FILE={json.dumps(bench_path or '')}",
                f"export CHIPLOOP_ATPG_METRICS_JSON={json.dumps(metrics_path)}",
                f"export CHIPLOOP_ATPG_PATTERNS_DIR={json.dumps(patterns_path)}",
                configured,
                "",
            ]),
        )
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


def _adapter_metrics(stage_dir: str) -> tuple[dict, str | None]:
    candidates = [
        os.path.join(stage_dir, "atpg_metrics.json"),
        os.path.join(stage_dir, "metrics", "atpg_metrics.json"),
        os.path.join(stage_dir, "reports", "atpg_metrics.json"),
    ]
    for path in candidates:
        data = _read_json(path)
        if data:
            return data, path
    return {}, None


def _metric(data: dict, *names: str) -> float | int | None:
    for name in names:
        value = _number_or_none(data.get(name))
        if value is not None:
            return value
    return None


def _safe_adapter_output_path(stage_dir: str, path: object) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = os.path.abspath(path)
    stage_root = os.path.abspath(stage_dir)
    if not candidate.startswith(stage_root + os.sep):
        return None
    return candidate if os.path.exists(candidate) else None


def _pattern_count_from_file(path: str | None) -> int | None:
    if not path or not os.path.exists(path):
        return None
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", "//", "*")):
                    continue
                if re.match(r"^(test|pattern)\b", line, flags=re.IGNORECASE):
                    count += 1
                elif re.search(r"[01xX]", line) and not re.search(r"\b(fault|coverage|collapsed|detected|undetected)\b", line, flags=re.IGNORECASE):
                    count += 1
    except Exception:
        return None
    return count if count > 0 else None


def _coverage_from_text(text: str) -> float | None:
    for pattern in (
        r"fault\s+coverage\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"stuck-at\s+coverage\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"coverage\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*%",
    ):
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _metrics_show_real_atpg_result(
    pattern_count: float | int | None,
    stuck_at_coverage_pct: float | int | None,
    faults_detected: float | int | None,
    faults_undetected: float | int | None,
    faults_aborted: float | int | None,
) -> bool:
    if pattern_count is not None and pattern_count > 0:
        return True
    if stuck_at_coverage_pct is not None:
        return True
    return any(value is not None and value > 0 for value in (faults_detected, faults_undetected, faults_aborted))


def _adapter_log_has_execution_error(log: str) -> bool:
    text = (log or "").lower()
    return any(
        phrase in text
        for phrase in (
            "no such file or directory",
            "command not found",
            "permission denied",
            "not executable",
            "syntax error",
        )
    )


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    digital = state.setdefault("digital", {})
    dft_state = digital.get("dft") if isinstance(digital.get("dft"), dict) else {}
    blocked_dft_statuses = {"not_applicable", "scan_not_inserted", "scan_cell_mapping_required", "no_scan_flops", "tool_unavailable"}
    if toggles.get("enable_scan_dft") is False or dft_state.get("status") in blocked_dft_statuses:
        workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
        stage_dir = os.path.join(workflow_dir, "digital", "atpg")
        _ensure_dir(stage_dir)
        if toggles.get("enable_scan_dft") is False:
            reason = "enable_scan_dft_disabled"
        elif dft_state.get("status") == "scan_cell_mapping_required":
            reason = "scan_cell_mapping_required"
        else:
            reason = "scan_dft_not_available"
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "not_applicable",
            "reason": reason,
            "tool": None,
            "pattern_count": None,
            "stuck_at_coverage_pct": None,
            "coverage_source": "not_applicable",
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        report = f"# Scan ATPG Coverage\n\n- Status: `not_applicable`\n- Reason: `{reason}`\n"
        _write_text(os.path.join(stage_dir, "atpg_summary.json"), json.dumps(summary, indent=2))
        _write_text(os.path.join(stage_dir, "atpg_report.md"), report)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_report.md", report)
        digital["atpg"] = {
            "status": "not_applicable",
            "reason": reason,
            "summary_json": os.path.join(stage_dir, "atpg_summary.json"),
        }
        state["status"] = f"{AGENT_NAME}: not_applicable"
        return state

    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "atpg")
    logs_dir = os.path.join(stage_dir, "logs")
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(logs_dir)
    _ensure_dir(input_dir)

    netlist = _scan_netlist(state, workflow_dir)
    input_netlist = os.path.join(input_dir, "scan_or_gate_netlist.v")
    input_bench = os.path.join(input_dir, "full_scan_atpg.bench")
    metrics_path = os.path.join(stage_dir, "atpg_metrics.json")
    bench_meta = {"status": "not_generated"}
    if netlist:
        netlist_text = _read_text(netlist)
        _write_text(input_netlist, netlist_text)
        bench_text, bench_meta = _generate_full_scan_bench(netlist_text)
        if bench_meta.get("status") == "generated":
            _write_text(input_bench, bench_text)

    tool_name, tool_exe, tool_diagnostics = _tool_choice(state, stage_dir)
    rc = None
    log = ""
    configured_atpg = bool(os.getenv("CHIPLOOP_ATPG_COMMAND", "").strip())
    if configured_atpg and not tool_name:
        tool_name = "configured_command"
        tool_exe = os.getenv("CHIPLOOP_ATPG_COMMAND", "").strip()

    if not netlist:
        status = "incomplete_inputs"
        log = "No scan-stitched DFT netlist found for ATPG.\n"
    elif bench_meta.get("status") != "generated":
        status = "atpg_bench_generation_failed"
        log = "Failed to generate Atalanta full-scan .bench collateral from scan netlist.\n"
        unsupported = bench_meta.get("unsupported_cells")
        if unsupported:
            log += "Unsupported cells:\n" + "\n".join(str(cell) for cell in unsupported) + "\n"
    elif not tool_name:
        status = "tool_unavailable"
        log = "No open-source ATPG tool found. Configure CHIPLOOP_FAULT, CHIPLOOP_ATALANTA, CHIPLOOP_PODEM, or CHIPLOOP_ATPG_COMMAND.\n"
        if tool_diagnostics:
            log += "\nSkipped non-ATPG tool matches:\n"
            for item in tool_diagnostics:
                log += f"- {item.get('tool')}: {item.get('status')} ({item.get('executable')})\n"
    else:
        rc, log = _run_detected_tool(state, tool_name, stage_dir, input_netlist, metrics_path, input_bench)
        if tool_name == "fault" and not configured_atpg and _is_wrong_fault_tool(log):
            status = "wrong_tool_detected"
            log += "\n\nThe detected `fault` executable is not a digital ATPG tool; it appears to be a network resilience/fault-injection CLI. Ignoring it for ATPG coverage.\n"
        else:
            status = "adapter_ready" if configured_atpg else "tool_detected_needs_adapter_command"
        if status != "wrong_tool_detected" and rc not in (0, None) and "usage" not in log.lower() and "help" not in log.lower():
            status = "tool_probe_failed"

    metrics, metrics_file = _adapter_metrics(stage_dir)
    pattern_count = _metric(metrics, "pattern_count", "patterns", "test_patterns")
    stuck_at_coverage_pct = _metric(metrics, "stuck_at_coverage_pct", "coverage_pct", "fault_coverage_pct", "coverage")
    faults_detected = _metric(metrics, "faults_detected", "detected_faults")
    faults_undetected = _metric(metrics, "faults_undetected", "undetected_faults")
    faults_aborted = _metric(metrics, "faults_aborted", "aborted_faults")
    pattern_file = _safe_adapter_output_path(stage_dir, metrics.get("pattern_file"))
    atalanta_output = _safe_adapter_output_path(stage_dir, metrics.get("atalanta_output"))
    inferred_pattern_count = _pattern_count_from_file(pattern_file)
    if (pattern_count is None or pattern_count == 0) and inferred_pattern_count is not None:
        pattern_count = inferred_pattern_count
    if stuck_at_coverage_pct is None and atalanta_output:
        stuck_at_coverage_pct = _coverage_from_text(_read_text(atalanta_output))
    if configured_atpg and rc == 0:
        if _adapter_log_has_execution_error(log) and not _metrics_show_real_atpg_result(pattern_count, stuck_at_coverage_pct, faults_detected, faults_undetected, faults_aborted):
            status = "adapter_failed"
        elif _metrics_show_real_atpg_result(pattern_count, stuck_at_coverage_pct, faults_detected, faults_undetected, faults_aborted):
            status = "patterns_generated"
        elif metrics_file:
            status = "adapter_completed_no_patterns"
        else:
            status = "adapter_completed_no_metrics"
    elif configured_atpg and rc not in (0, None):
        if rc == 127 or "no such file or directory" in log.lower() or "does not exist" in log.lower():
            status = "adapter_command_missing"
        else:
            status = "adapter_failed"

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
        "pattern_count": pattern_count,
        "stuck_at_coverage_pct": stuck_at_coverage_pct,
        "faults_detected": faults_detected,
        "faults_undetected": faults_undetected,
        "faults_aborted": faults_aborted,
        "coverage_source": "configured_adapter_metrics" if status == "patterns_generated" else "not_reported",
        "metrics_file": os.path.basename(metrics_file) if metrics_file else None,
        "bench_generation": bench_meta,
        "tool_diagnostics": tool_diagnostics,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "summary": "digital/atpg/atpg_summary.json",
            "report": "digital/atpg/atpg_report.md",
            "log": "digital/atpg/logs/scan_atpg.log",
            "input_netlist": "digital/atpg/input/scan_or_gate_netlist.v" if netlist else None,
            "bench": "digital/atpg/input/full_scan_atpg.bench" if bench_meta.get("status") == "generated" else None,
            "metrics": "digital/atpg/atpg_metrics.json" if metrics_file else None,
            "patterns": f"digital/atpg/patterns/{os.path.basename(pattern_file)}" if pattern_file else None,
            "tool_output": f"digital/atpg/{os.path.basename(atalanta_output)}" if atalanta_output else None,
        },
    }
    report = "\n".join([
        "# Scan ATPG Coverage",
        "",
        f"- Status: `{status}`",
        f"- Tool: `{tool_name or 'not found'}`",
        f"- Input netlist: `{os.path.basename(netlist) if netlist else 'missing'}`",
        f"- Generated bench: `{bench_meta.get('status')}`",
        f"- Pattern count: `{pattern_count if pattern_count is not None else 'not reported'}`",
        f"- Stuck-at coverage: `{stuck_at_coverage_pct if stuck_at_coverage_pct is not None else 'not reported'}`",
        f"- Faults detected: `{faults_detected if faults_detected is not None else 'not reported'}`",
        f"- Faults undetected: `{faults_undetected if faults_undetected is not None else 'not reported'}`",
        f"- Faults aborted: `{faults_aborted if faults_aborted is not None else 'not reported'}`",
        "",
        "A configured ATPG adapter must write `atpg_metrics.json` with real pattern and coverage metrics. Runs without that file are reported as `adapter_completed_no_metrics`; zero-pattern metrics are reported as `adapter_completed_no_patterns`.",
        "If status is `wrong_tool_detected`, the executable name matched but the binary is not a digital ATPG tool.",
    ]) + "\n"
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    if netlist:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/input/scan_or_gate_netlist.v", _read_text(input_netlist))
    if bench_meta.get("status") == "generated":
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/input/full_scan_atpg.bench", _read_text(input_bench))
    if metrics_file:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_metrics.json", _read_text(metrics_file))
    if pattern_file:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"atpg/patterns/{os.path.basename(pattern_file)}", _read_text(pattern_file))
    if atalanta_output:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"atpg/{os.path.basename(atalanta_output)}", _read_text(atalanta_output))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/logs/scan_atpg.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "atpg/atpg_report.md", report)

    digital["atpg"] = {
        "status": status,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
    }
    state["status"] = f"{AGENT_NAME}: {status}"
    return state
