import json
import os
import re
from pathlib import Path
from typing import Any

from agents.digital.digital_logic_equivalence_agent import (
    _classify,
    _generated_stdcell_model,
    _missing_stdcell_models,
    _module_name_in_file,
    _module_instance_pins,
    _module_ports_from_text,
    _prepare_golden_rtl_for_yosys,
    _prepare_stdcell_models_for_yosys,
    _referenced_non_stdcell_modules,
    _rtl_sources,
    _read_text,
    _stdcell_verilog_candidates,
    _top_module,
)
from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Digital Post-DFT Logic Equivalence Check Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    Path(path).write_text(content, encoding="utf-8")


def _existing_path(value: Any, workflow_dir: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        path = os.path.abspath(cand)
        if os.path.exists(path):
            return path
    return None


def _synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for value in (synth.get("netlist"), digital.get("synth_netlist"), state.get("synth_netlist")):
        path = _existing_path(value, workflow_dir)
        if path:
            return path
    netlist_dir = os.path.join(workflow_dir, "digital", "synth", "netlist")
    hits = sorted(str(p) for p in Path(netlist_dir).glob("*.v")) if os.path.isdir(netlist_dir) else []
    return hits[0] if hits else None


def _post_dft_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    dft = digital.get("dft") if isinstance(digital.get("dft"), dict) else {}
    for value in (
        dft.get("scan_stitched_netlist"),
        dft.get("post_dft_netlist"),
        digital.get("post_dft_netlist"),
        state.get("post_dft_netlist"),
    ):
        path = _existing_path(value, workflow_dir)
        if path:
            return path
    for rel in ("digital/dft/scan_stitched_netlist.v", "digital/dft/input/scan_mapped_netlist.v"):
        path = _existing_path(rel, workflow_dir)
        if path:
            return path
    return None


def _ports(path: str | None, top: str) -> dict[str, str]:
    if not path:
        return {}
    return {name: direction for name, direction in _module_ports_from_text(_read_text(path), top)}


def _extra_gate_ports(golden: str | None, gate: str | None, top: str) -> list[str]:
    golden_ports = _ports(golden, top)
    gate_ports = _ports(gate, top)
    extras = [name for name in gate_ports if name not in golden_ports]
    return [name for name in extras if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name)]


def _fallback_macro_blackboxes(netlist: str | None, stage_dir: str, top: str, existing: list[str]) -> list[str]:
    existing_names = {_module_name_in_file(path) for path in existing}
    stubs: list[str] = []
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(input_dir)
    for module_name in _referenced_non_stdcell_modules(netlist, top):
        if not module_name or module_name in existing_names:
            continue
        pins = _module_instance_pins(netlist, module_name)
        if not pins:
            continue
        lines = [
            "// Auto-generated blackbox stub for preserved macro Post-DFT LEC.",
            "(* blackbox *)",
            f"module {module_name}({', '.join(pins)});",
        ]
        for pin in pins:
            lines.append(f"  inout {pin};")
        lines.append("endmodule")
        path = os.path.join(input_dir, f"{module_name}_blackbox.v")
        _write_text(path, "\n".join(lines) + "\n")
        stubs.append(path)
    return stubs


def _combined_cell_reference_netlist(golden: str | None, gate: str | None, stage_dir: str) -> str | None:
    texts = [_read_text(path) for path in (golden, gate) if path]
    combined = "\n".join(text for text in texts if text.strip())
    if not combined.strip():
        return None
    out = os.path.join(stage_dir, "input", "cell_reference_netlists.v")
    _write_text(out, combined)
    return out


def _gate_to_gate_yosys_script(
    golden: str,
    gate: str,
    top: str,
    stdcell_verilog: list[str],
    ignored_gate_ports: list[str],
    macro_blackbox_verilog: list[str] | None = None,
) -> str:
    read_models = "\n".join(f"read_verilog -sv -D FUNCTIONAL {json.dumps(path)}" for path in stdcell_verilog)
    read_macro_blackboxes = "\n".join(f"read_verilog -sv {json.dumps(path)}" for path in (macro_blackbox_verilog or []))
    delete_ports = ""
    if ignored_gate_ports:
        delete_ports = "delete -port " + " ".join(f"w:{port}" for port in ignored_gate_ports) + "\n"
    return f"""# Auto-generated by {AGENT_NAME}
{read_models}
{read_macro_blackboxes}
read_verilog -sv {json.dumps(golden)}
hierarchy -check -top {top}
proc; opt; memory; opt
async2sync
flatten
splitnets -ports
opt_clean
rename -top gold
design -stash gold

design -reset
{read_models}
{read_macro_blackboxes}
read_verilog -sv {json.dumps(gate)}
hierarchy -check -top {top}
proc; opt; memory; opt
async2sync
flatten
{delete_ports}\
splitnets -ports
opt_clean
rename -top gate
design -stash gate

design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple -seq 20
equiv_induct -undef -seq 256
equiv_simple -seq 50
equiv_status -assert
"""


def _failure_reason(
    verdict: str,
    yosys: str | None,
    golden: str | None,
    gate: str | None,
    stdcell_verilog: list[str],
    missing_cells: list[str],
    unproven: int,
) -> str:
    if verdict == "pass":
        return "equivalence_proven"
    if not yosys:
        return "yosys_not_available_in_tool_profile"
    if not golden:
        return "missing_synthesis_netlist"
    if not gate:
        return "missing_post_dft_netlist"
    if not stdcell_verilog:
        return "missing_stdcell_models"
    if missing_cells:
        return "missing_stdcell_models_for_post_dft_lec"
    if unproven:
        return "equivalence_points_unproven"
    return "yosys_inconclusive_see_post_dft_lec_log"


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "post_dft_lec")
    logs_dir = os.path.join(stage_dir, "logs")
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(logs_dir)
    _ensure_dir(input_dir)

    golden = _synth_netlist(state, workflow_dir)
    gate = _post_dft_netlist(state, workflow_dir)
    top = _top_module(state, [], golden or gate)
    if top == "top":
        top = _module_name_in_file(golden) or _module_name_in_file(gate) or top
    yosys = tool_path("yosys", state)
    cell_reference = _combined_cell_reference_netlist(golden, gate, stage_dir)
    generated = _generated_stdcell_model(cell_reference or gate or golden, stage_dir)
    stdcell_verilog = [generated] if generated else _prepare_stdcell_models_for_yosys(_stdcell_verilog_candidates(state, workflow_dir), stage_dir)
    missing_cells = _missing_stdcell_models(cell_reference or gate or golden, stdcell_verilog)
    ignored_gate_ports = _extra_gate_ports(golden, gate, top)
    rtl_files = _rtl_sources(state, workflow_dir)
    _prepared_rtl_files, macro_blackboxes = _prepare_golden_rtl_for_yosys(rtl_files, gate or golden, stage_dir, top) if rtl_files and (gate or golden) else (rtl_files, [])
    macro_blackboxes = list(dict.fromkeys(macro_blackboxes + _fallback_macro_blackboxes(gate or golden, stage_dir, top, macro_blackboxes)))

    script_path = os.path.join(stage_dir, "yosys_post_dft_lec.ys")
    log_path = os.path.join(logs_dir, "yosys_post_dft_lec.log")
    summary_path = os.path.join(stage_dir, "post_dft_lec_summary.json")
    has_inputs = bool(golden and gate and yosys and stdcell_verilog and not missing_cells)
    script = ""
    log = ""
    rc = None
    if has_inputs:
        script = _gate_to_gate_yosys_script(golden, gate, top, stdcell_verilog, ignored_gate_ports, macro_blackboxes)
        _write_text(script_path, script)
        proc = run_command(state, "post_dft_logic_equivalence", ["yosys", "-s", script_path], cwd=stage_dir, timeout_sec=900)
        rc = proc.returncode
        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    elif not yosys:
        log = "Yosys is not available in this tool profile.\n"
    elif not golden:
        log = "Missing synthesized netlist for Post-DFT LEC.\n"
    elif not gate:
        log = "Missing scan-stitched/post-DFT netlist for Post-DFT LEC.\n"
    elif not stdcell_verilog:
        log = "Missing standard-cell functional models for Post-DFT LEC.\n"
    else:
        log = "Missing standard-cell models referenced by post-DFT netlist: " + ", ".join(missing_cells[:20]) + "\n"
    _write_text(log_path, log)

    verdict, unproven = _classify(rc, log, bool(yosys))
    if missing_cells:
        verdict = "inconclusive"
    reason = _failure_reason(verdict, yosys, golden, gate, stdcell_verilog, missing_cells, unproven)
    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": verdict,
        "tool": "yosys",
        "tool_available": bool(yosys),
        "return_code": rc,
        "top_module": top,
        "synthesis_netlist": os.path.basename(golden) if golden else None,
        "post_dft_netlist": os.path.basename(gate) if gate else None,
        "ignored_gate_only_ports": ignored_gate_ports,
        "macro_blackbox_stubs": [os.path.basename(path) for path in macro_blackboxes],
        "stdcell_model_files": [os.path.basename(path) for path in stdcell_verilog],
        "stdcell_model_reference": os.path.basename(cell_reference) if cell_reference else None,
        "missing_stdcell_models": missing_cells[:50],
        "unproven_points": unproven,
        "failure_reason": reason,
        "artifacts": {
            "script": "digital/post_dft_lec/yosys_post_dft_lec.ys" if script else None,
            "log": "digital/post_dft_lec/logs/yosys_post_dft_lec.log",
            "summary": "digital/post_dft_lec/post_dft_lec_summary.json",
        },
    }
    report = "\n".join([
        "# Digital Post-DFT Logic Equivalence Check",
        "",
        f"- Status: `{verdict}`",
        f"- Failure reason: `{reason}`",
        f"- Top module: `{top}`",
        f"- Synthesis netlist: `{os.path.basename(golden) if golden else 'missing'}`",
        f"- Post-DFT netlist: `{os.path.basename(gate) if gate else 'missing'}`",
        f"- Unproven points: `{unproven}`",
    ]) + "\n"
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(os.path.join(stage_dir, "post_dft_lec_report.md"), report)

    if script:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "post_dft_lec/yosys_post_dft_lec.ys", script)
    for stub_path in macro_blackboxes:
        save_text_artifact_and_record(
            workflow_id,
            AGENT_NAME,
            "digital",
            f"post_dft_lec/input/{os.path.basename(stub_path)}",
            _read_text(stub_path),
        )
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "post_dft_lec/logs/yosys_post_dft_lec.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "post_dft_lec/post_dft_lec_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "post_dft_lec/post_dft_lec_report.md", report)

    digital = state.setdefault("digital", {})
    digital["post_dft_lec"] = {"status": verdict, "summary_json": summary_path, "log": log_path, "unproven_points": unproven}
    state["status"] = f"{AGENT_NAME}: {verdict}"
    return state
