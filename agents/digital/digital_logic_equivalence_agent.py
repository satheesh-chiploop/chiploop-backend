import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Logic Equivalence Check Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


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


def _dedupe(paths: list[str]) -> list[str]:
    out = []
    seen = set()
    for path in paths:
        ap = os.path.abspath(path)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def _read_json(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pdk_root_host(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    foundry_path = _existing_path(os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"))
    foundry_file = _read_json(foundry_path)
    candidates = [
        state.get("pdk_root_host"),
        state.get("pdk_root"),
        foundry.get("pdk_root"),
        foundry_file.get("pdk_root"),
        os.getenv("CHIPLOOP_PDK_ROOT_HOST"),
        os.getenv("PDK_ROOT"),
        "/root/chiploop-backend/backend/pdk",
        "backend/pdk",
    ]
    for cand in candidates:
        path = _existing_path(cand)
        if path:
            return path
    return None


def _pdk_variant(state: dict, workflow_dir: str) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    foundry_path = _existing_path(os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"))
    foundry_file = _read_json(foundry_path)
    value = (
        state.get("pdk_variant")
        or state.get("pdk")
        or foundry.get("pdk")
        or foundry_file.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or os.getenv("PDK")
        or "sky130A"
    )
    return str(value).strip() or "sky130A"


def _prefer_active_pdk(paths: list[str], pdk_variant: str) -> list[str]:
    preferred = [p for p in paths if f"{os.sep}{pdk_variant}{os.sep}" in os.path.abspath(p)]
    return preferred or paths


def _liberty_candidates(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    explicit: list[str] = []
    for value in (
        state.get("liberty"),
        state.get("stdcell_liberty"),
        state.get("timing_liberty"),
        digital.get("liberty"),
        digital.get("stdcell_liberty"),
    ):
        path = _existing_path(value, workflow_dir)
        if path:
            explicit.append(path)

    root = _pdk_root_host(state, workflow_dir)
    pdk_variant = _pdk_variant(state, workflow_dir)
    discovered: list[str] = []
    if root:
        patterns = [
            os.path.join(root, pdk_variant, "libs.ref", "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
            os.path.join(root, "**", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
            os.path.join(root, "**", "sky130_fd_sc_hd__tt*.lib"),
            os.path.join(root, "**", "*tt*025C*1v80*.lib"),
            os.path.join(root, "**", "*.lib"),
        ]
        for pattern in patterns:
            discovered.extend(glob.glob(pattern, recursive=True)[:10])
            if discovered:
                break
    return _dedupe(explicit + _prefer_active_pdk(discovered, pdk_variant))


def _stdcell_verilog_candidates(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    explicit: list[str] = []
    for value in (
        state.get("stdcell_verilog"),
        state.get("stdcell_model"),
        digital.get("stdcell_verilog"),
        digital.get("stdcell_model"),
    ):
        path = _existing_path(value, workflow_dir)
        if path:
            explicit.append(path)

    root = _pdk_root_host(state, workflow_dir)
    pdk_variant = _pdk_variant(state, workflow_dir)
    discovered: list[str] = []
    if root:
        direct = [
            os.path.join(root, pdk_variant, "libs.ref", "sky130_fd_sc_hd", "verilog", "primitives.v"),
            os.path.join(root, pdk_variant, "libs.ref", "sky130_fd_sc_hd", "verilog", "sky130_fd_sc_hd.v"),
        ]
        direct_existing = [path for path in direct if os.path.exists(path)]
        if direct_existing:
            return _dedupe(explicit + direct_existing)
        patterns = [
            os.path.join(root, "**", "cells_sim.v"),
            os.path.join(root, "**", "cells.functional.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.functional.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.behavioral.v"),
            os.path.join(root, "**", "libs.ref", "sky130_fd_sc_hd", "verilog", "*.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.v"),
        ]
        for pattern in patterns:
            discovered.extend(glob.glob(pattern, recursive=True)[:200])
            if discovered:
                break
    discovered = [p for p in _prefer_active_pdk(discovered, pdk_variant) if "blackbox" not in os.path.basename(p).lower()]
    return _dedupe(explicit + discovered)


def _referenced_modules(verilog_path: str | None) -> list[str]:
    if not verilog_path:
        return []
    try:
        text = Path(verilog_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    refs: list[str] = []
    for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()", text, flags=re.MULTILINE):
        name = match.group(1)
        if name in {"module", "endmodule", "assign", "always", "if", "for", "generate"}:
            continue
        refs.append(name)
    return list(dict.fromkeys(refs))


def _module_names_in_files(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        names.update(re.findall(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE))
    return names


def _referenced_sky130_cells(gate: str | None) -> list[str]:
    return [name for name in _referenced_modules(gate) if name.startswith("sky130_fd_sc_")]


def _missing_stdcell_models(gate: str | None, existing_models: list[str]) -> list[str]:
    referenced = _referenced_sky130_cells(gate)
    modeled_by_pdk = _module_names_in_files(existing_models)
    return [cell for cell in referenced if cell not in modeled_by_pdk]


def _rtl_sources(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    candidates: list[str] = []
    for value in (
        state.get("rtl_files"),
        state.get("rtl_inputs"),
        state.get("source_rtl_files"),
        digital.get("rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend([p for p in (_existing_path(x, workflow_dir) for x in value) if p])
    if not candidates:
        for pattern in (
            "handoff/rtl/**/*.v",
            "handoff/rtl/**/*.sv",
            "digital/handoff/rtl/**/*.v",
            "digital/handoff/rtl/**/*.sv",
            "digital/rtl/**/*.v",
            "digital/rtl/**/*.sv",
        ):
            candidates.extend(glob.glob(os.path.join(workflow_dir, pattern), recursive=True))
    synth_dir = os.path.join(workflow_dir, "digital", "synth")
    return [p for p in _dedupe(candidates) if not os.path.abspath(p).startswith(os.path.abspath(synth_dir))]


def _synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (
        synth.get("netlist"),
        digital.get("synth_netlist"),
        state.get("synth_netlist"),
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    hits = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    return hits[0] if hits else None


def _top_module(state: dict, rtl_files: list[str], netlist: str | None) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for value in (synth.get("top_module"), digital.get("top_module"), state.get("top_module")):
        if isinstance(value, str) and value.strip() and value.strip() != "imported_from_arch2rtl":
            return value.strip()
    for path in [netlist, *rtl_files]:
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        match = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE)
        if match:
            return match.group(1)
    return "top"


def _yosys_script(golden: list[str], gate: str, top: str, stdcell_verilog: list[str]) -> str:
    read_golden = "\n".join(f"read_verilog -sv {json.dumps(path)}" for path in golden)
    read_models = "\n".join(f"read_verilog -sv {json.dumps(path)}" for path in stdcell_verilog)
    return f"""# Auto-generated by {AGENT_NAME}
{read_golden}
hierarchy -check -top {top}
proc; opt; memory; opt
flatten
splitnets -ports
opt_clean
rename -top gold
design -stash gold

{read_models}
read_verilog -sv {json.dumps(gate)}
hierarchy -check -top {top}
proc; opt; memory; opt
flatten
splitnets -ports
opt_clean
rename -top gate
design -stash gate

design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple -seq 10
equiv_induct -undef
equiv_simple -seq 10
equiv_status -assert
"""


def _classify(returncode: int | None, log: str, tool_available: bool) -> tuple[str, int]:
    if not tool_available:
        return "tool_unavailable", 0
    text = log.lower()
    if returncode == 0 and "equivalence successfully proven" in text:
        return "pass", 0
    if returncode == 0 and "unproven" not in text and "failed" not in text:
        return "pass", 0
    unproven = 0
    for match in re.finditer(r"(\d+)\s+unproven", text):
        try:
            unproven += int(match.group(1))
        except Exception:
            pass
    if "equivalence checking failed" in text or "assert failed" in text:
        return "fail", unproven or 1
    if "no sat model available" in text:
        return "inconclusive_missing_sat_models", unproven
    if "is not part of the design" in text or ("module `" in text and "referenced" in text):
        return "inconclusive_unresolved_cells", unproven
    if "blackbox" in text and ("sat" in text or "equiv" in text):
        return "inconclusive_missing_sat_models", unproven
    return "inconclusive", unproven


def _failure_reason(verdict: str, *, rtl_files: list[str], netlist: str | None, yosys: str | None, liberty_files: list[str], stdcell_verilog: list[str], missing_cells: list[str], unproven: int) -> str:
    if verdict == "pass":
        return "equivalence_proven"
    if not yosys:
        return "yosys_not_available_in_tool_profile"
    if not rtl_files:
        return "missing_rtl_inputs"
    if not netlist:
        return "missing_synthesized_netlist"
    if not stdcell_verilog:
        return "missing_standard_cell_verilog_models"
    if missing_cells:
        return "standard_cell_verilog_models_incomplete"
    if unproven:
        return "equivalence_points_unproven"
    if verdict.startswith("inconclusive"):
        return "yosys_inconclusive_see_lec_log"
    if verdict == "fail":
        return "rtl_gate_mismatch_or_unsupported_sequential_equivalence"
    return verdict


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "lec")
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(logs_dir)

    rtl_files = _rtl_sources(state, workflow_dir)
    netlist = _synth_netlist(state, workflow_dir)
    top = _top_module(state, rtl_files, netlist)
    yosys = tool_path("yosys", state)
    liberty_files = _liberty_candidates(state, workflow_dir)
    stdcell_verilog = _stdcell_verilog_candidates(state, workflow_dir)
    missing_stdcell_models = _missing_stdcell_models(netlist, stdcell_verilog)
    model_strategy = "pdk_stdcell_verilog" if stdcell_verilog else "none"

    script_path = os.path.join(stage_dir, "yosys_lec.ys")
    log_path = os.path.join(logs_dir, "yosys_lec.log")
    report_path = os.path.join(stage_dir, "lec_report.md")
    summary_path = os.path.join(stage_dir, "lec_summary.json")

    has_required_inputs = bool(rtl_files and netlist and yosys and stdcell_verilog and not missing_stdcell_models)
    if rtl_files and netlist:
        script = _yosys_script(rtl_files, netlist, top, stdcell_verilog)
    else:
        script = "# Missing RTL or synthesized netlist; LEC not run.\n"
    _write_text(script_path, script)

    rc = None
    log = ""
    if has_required_inputs:
        proc = run_command(state, "logic_equivalence", ["yosys", "-s", script_path], cwd=stage_dir, timeout_sec=900)
        rc = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    elif not yosys:
        log = "Yosys executable was not found in the active ChipLoop tool profile.\n"
    elif not stdcell_verilog:
        log = "Missing real standard-cell Verilog simulation/functional models for synthesized gate netlist LEC.\n"
    elif missing_stdcell_models:
        log = "Standard-cell Verilog model coverage is incomplete for synthesized gate netlist LEC.\nMissing cells:\n" + "\n".join(missing_stdcell_models) + "\n"
    else:
        log = "Missing RTL or synthesized netlist for LEC.\n"
    _write_text(log_path, log)

    verdict, unproven = _classify(rc, log, bool(yosys))
    if not rtl_files or not netlist:
        verdict = "incomplete_inputs"
    if not stdcell_verilog:
        verdict = "missing_stdcell_models"
    if missing_stdcell_models:
        verdict = "incomplete_stdcell_models"
    failure_reason = _failure_reason(
        verdict,
        rtl_files=rtl_files,
        netlist=netlist,
        yosys=yosys,
        liberty_files=liberty_files,
        stdcell_verilog=stdcell_verilog,
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
        "rtl_files": [os.path.basename(p) for p in rtl_files],
        "synth_netlist": os.path.basename(netlist) if netlist else None,
        "liberty_files": [os.path.basename(p) for p in liberty_files],
        "liberty_file_count": len(liberty_files),
        "liberty_usage": "discovered_not_required_for_verilog_model_lec",
        "stdcell_verilog_files": [os.path.basename(p) for p in stdcell_verilog],
        "stdcell_verilog_file_count": len(stdcell_verilog),
        "stdcell_model_strategy": model_strategy,
        "missing_stdcell_models": missing_stdcell_models,
        "missing_stdcell_model_count": len(missing_stdcell_models),
        "unproven_points": unproven,
        "failure_reason": failure_reason,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "script": "digital/lec/yosys_lec.ys",
            "log": "digital/lec/logs/yosys_lec.log",
            "summary": "digital/lec/lec_summary.json",
            "report": "digital/lec/lec_report.md",
        },
    }
    report = "\n".join([
        "# Logic Equivalence Check",
        "",
        f"- Status: `{verdict}`",
        f"- Tool: `yosys`",
        f"- Top module: `{top}`",
        f"- RTL files: `{len(rtl_files)}`",
        f"- Synth netlist: `{os.path.basename(netlist) if netlist else 'missing'}`",
        f"- Liberty files discovered: `{len(liberty_files)}`",
        f"- Standard-cell Verilog models loaded: `{len(stdcell_verilog)}`",
        f"- Standard-cell model strategy: `{model_strategy}`",
        f"- Missing standard-cell models: `{len(missing_stdcell_models)}`",
        f"- Unproven points: `{unproven}`",
        f"- Return code: `{rc}`",
        f"- Failure reason: `{failure_reason}`",
        "",
        "If this is inconclusive, inspect `digital/lec/logs/yosys_lec.log` and `digital/lec/lec_summary.json` for unsupported cells, black boxes, or reset/initial-state assumptions.",
    ]) + "\n"
    if missing_stdcell_models:
        report += "\n## Missing Standard-Cell Models\n\n" + "\n".join(f"- `{cell}`" for cell in missing_stdcell_models) + "\n"
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/yosys_lec.ys", script)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/logs/yosys_lec.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/lec_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/lec_report.md", report)

    digital = state.setdefault("digital", {})
    digital["lec"] = {
        "status": verdict,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
        "script": script_path,
    }
    state["status"] = f"{AGENT_NAME}: {verdict}"
    return state
