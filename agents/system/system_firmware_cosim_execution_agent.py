import json
import os
import re
import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record


# ---------------------------------------------------------------------
# System Loop: Firmware CoSim Execution Agent
# - Consumes assembled SoC top + firmware/cosim collateral
# - Produces execution summary artifacts for System_Firmware
# - Deliberately artifact-first and environment-safe:
#   does not hard-fail if simulator/tool runtime is unavailable
# - Avoids claiming real execution when required runtime inputs are missing
# ---------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _norm_path(p: Any) -> str:
    if p is None:
        return ""
    return str(p).strip().replace("\\", "/")


def _basename_no_ext(p: str) -> str:
    b = os.path.basename(_norm_path(p))
    if "." in b:
        return ".".join(b.split(".")[:-1]) or b
    return b


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out = []
    seen = set()
    for x in items:
        x = _norm_path(x)
        if not x or x in seen:
            continue
        out.append(x)
        seen.add(x)
    return out


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _state_get_first_path(state: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = state.get(k)
        if _is_nonempty_str(v):
            return _norm_path(v)
    return ""


def _state_collect_paths(state: Dict[str, Any], keys: List[str]) -> List[str]:
    found = []
    for k in keys:
        v = state.get(k)
        if isinstance(v, list):
            for item in v:
                if _is_nonempty_str(item):
                    found.append(_norm_path(item))
        elif _is_nonempty_str(v):
            found.append(_norm_path(v))
    return _dedupe_keep_order(found)


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _looks_like_sv(p: str) -> bool:
    p = _norm_path(p).lower()
    return p.endswith(".sv") or p.endswith(".v")


def _find_soc_top_path(state: Dict[str, Any]) -> str:
    candidates = [
        state.get("soc_top_sim_path"),
        state.get("system_top_sim_path"),
        state.get("top_sim_path"),
        state.get("assembled_soc_top_path"),
        state.get("system_firmware_soc_top_path"),
    ]
    for c in candidates:
        if _is_nonempty_str(c):
            return _norm_path(c)

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        lp = p.lower()
        if lp.endswith("_sim.sv") and "system/" in lp:
            return p
    return ""


def _find_elf_path(state: Dict[str, Any]) -> str:
    candidates = [
        state.get("firmware_elf_path"),
        state.get("elf_path"),
        state.get("embedded_elf_path"),
        state.get("system_firmware_elf_path"),
    ]
    for c in candidates:
        if _is_nonempty_str(c):
            return _norm_path(c)

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        if p.lower().endswith(".elf"):
            return p
    return ""


def _find_makefile_path(state: Dict[str, Any]) -> str:
    candidates = [
        state.get("digital_tb_makefile_path"),
        state.get("makefile_path"),
        state.get("cocotb_makefile_path"),
        state.get("embedded_cocotb_makefile_path"),
    ]
    for c in candidates:
        if _is_nonempty_str(c):
            return _norm_path(c)

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        if os.path.basename(p).lower() == "makefile":
            return p
    return ""


def _find_test_paths(state: Dict[str, Any]) -> List[str]:
    candidates = _state_collect_paths(
        state,
        [
            "digital_test_paths",
            "test_paths",
            "cocotb_test_paths",
            "embedded_cocotb_test_paths",
        ],
    )

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        b = os.path.basename(p).lower()
        if b.startswith("test_") and b.endswith(".py"):
            candidates.append(_norm_path(p))
    return _dedupe_keep_order(candidates)


def _find_coverage_model_path(state: Dict[str, Any]) -> str:
    candidates = [
        state.get("coverage_model_path"),
        state.get("digital_coverage_model_path"),
        state.get("embedded_coverage_model_path"),
    ]
    for c in candidates:
        if _is_nonempty_str(c):
            return _norm_path(c)

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        if os.path.basename(p).lower() == "coverage_model.py":
            return p
    return ""


def _find_assertions_path(state: Dict[str, Any]) -> str:
    candidates = [
        state.get("assertions_path"),
        state.get("digital_assertions_path"),
        state.get("digital_sva_assertions_path"),
    ]
    for c in candidates:
        if _is_nonempty_str(c):
            return _norm_path(c)

    artifact_candidates = _state_collect_paths(
        state,
        [
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    for p in artifact_candidates:
        b = os.path.basename(p).lower()
        if b == "assertions.sv" or "assert" in b:
            return p
    return ""


def _find_verilog_inputs(state: Dict[str, Any], soc_top_path: str) -> List[str]:
    candidates = _state_collect_paths(
        state,
        [
            "rtl_files",
            "verilog_files",
            "sv_files",
            "digital_rtl_files",
            "system_rtl_files",
            "generated_files",
            "artifacts",
            "artifact_paths",
            "output_files",
            "produced_artifacts",
            "files",
        ],
    )
    out = []
    for p in candidates:
        if _looks_like_sv(p):
            out.append(_norm_path(p))
    if soc_top_path:
        out.append(_norm_path(soc_top_path))
    return _dedupe_keep_order(out)


def _readiness(required: Dict[str, Any]) -> Dict[str, Any]:
    missing = []
    present = []

    for name, value in required.items():
        ok = False
        if isinstance(value, list):
            ok = len(value) > 0
        else:
            ok = bool(str(value).strip()) if value is not None else False

        if ok:
            present.append(name)
        else:
            missing.append(name)

    status = "ready" if not missing else "blocked"

    return {
        "status": status,
        "present": present,
        "missing": missing,
    }


def _default_test_matrix(test_paths: List[str]) -> List[Dict[str, Any]]:
    seeds = [1, 2]
    tests = test_paths or ["test_system_firmware_smoke.py"]

    matrix = []
    for test_path in tests:
        tname = os.path.basename(test_path)
        for seed in seeds:
            matrix.append(
                {
                    "test_name": tname,
                    "seed": seed,
                    "status": "planned",
                    "runtime_seconds": None,
                    "failure_reason": None,
                }
            )
    return matrix


def _build_notes(readiness: Dict[str, Any], optional_inputs: Dict[str, Any]) -> List[str]:
    notes = []

    if readiness["status"] != "ready":
        notes.append(
            "Execution was not attempted because one or more required inputs were missing."
        )

    if optional_inputs.get("coverage_model"):
        notes.append("Functional coverage model was detected and can be consumed by downstream summary.")
    else:
        notes.append("No coverage model detected; downstream summary should avoid reporting functional coverage percentages.")

    if optional_inputs.get("assertions"):
        notes.append("Digital assertions collateral was detected and can be referenced by downstream summary.")
    else:
        notes.append("No digital assertions collateral detected; assertion coverage should remain unavailable, not fabricated.")

    if optional_inputs.get("elf"):
        notes.append("Firmware ELF was detected for firmware-aware co-simulation.")
    else:
        notes.append("Firmware ELF was not detected; this run should be treated as not executable, not as a passing simulation.")

    return notes


def _markdown_report(summary: Dict[str, Any]) -> str:
    readiness = summary.get("readiness", {})
    tests = summary.get("test_matrix", [])
    inputs = summary.get("inputs", {})
    notes = summary.get("notes", [])

    lines = []
    lines.append("# System Firmware CoSim Execution Summary")
    lines.append("")
    lines.append(f"- Generated at: {summary.get('generated_at')}")
    lines.append(f"- Execution mode: {summary.get('execution_mode')}")
    lines.append(f"- Overall status: {summary.get('overall_status')}")
    lines.append(f"- Readiness: {readiness.get('status')}")
    lines.append("")

    lines.append("## Key Inputs")
    lines.append("")
    lines.append(f"- SoC top: `{inputs.get('soc_top_sim_path') or '(missing)'}`")
    lines.append(f"- Firmware ELF: `{inputs.get('firmware_elf_path') or '(missing)'}`")
    lines.append(f"- Cocotb Makefile: `{inputs.get('makefile_path') or '(missing)'}`")
    lines.append(f"- Test files: `{len(inputs.get('test_paths') or [])}`")
    lines.append(f"- Verilog/SystemVerilog files: `{len(inputs.get('rtl_inputs') or [])}`")
    lines.append("")

    if readiness.get("missing"):
        lines.append("## Missing Required Inputs")
        lines.append("")
        for m in readiness["missing"]:
            lines.append(f"- {m}")
        lines.append("")

    lines.append("## Planned Test Matrix")
    lines.append("")
    if tests:
        for t in tests:
            lines.append(
                f"- {t.get('test_name')} | seed={t.get('seed')} | status={t.get('status')}"
            )
    else:
        lines.append("- No tests discovered.")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for n in notes:
        lines.append(f"- {n}")
    lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    if summary.get("overall_status") == "ready_for_execution":
        lines.append(
            "All required execution inputs were detected. This artifact indicates the co-simulation bundle is ready for execution."
        )
    else:
        lines.append(
            "The co-simulation bundle is incomplete. Downstream agents should treat coverage as unavailable rather than infer passing results."
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_agent(state: dict) -> dict:
    agent_name = "System Firmware CoSim Execution Agent"
    workflow_id = state.get("workflow_id")

    print("\n⚙️ Running System Firmware CoSim Execution Agent")

    soc_top_sim_path = _find_soc_top_path(state)
    firmware_elf_path = _find_elf_path(state)
    makefile_path = _find_makefile_path(state)
    test_paths = _find_test_paths(state)
    coverage_model_path = _find_coverage_model_path(state)
    assertions_path = _find_assertions_path(state)
    rtl_inputs = _find_verilog_inputs(state, soc_top_sim_path)

    required = {
        "soc_top_sim_path": soc_top_sim_path,
        "firmware_elf_path": firmware_elf_path,
        "makefile_path": makefile_path,
        "test_paths": test_paths,
        "rtl_inputs": rtl_inputs,
    }
    readiness = _readiness(required)

    optional_inputs = {
        "coverage_model": coverage_model_path,
        "assertions": assertions_path,
        "elf": firmware_elf_path,
    }

    execution_mode = "artifact_readiness_only"
    overall_status = "ready_for_execution" if readiness["status"] == "ready" else "blocked_missing_inputs"

    test_matrix = _default_test_matrix(test_paths)

    summary = {
        "agent": agent_name,
        "generated_at": _now(),
        "execution_mode": execution_mode,
        "overall_status": overall_status,
        "readiness": readiness,
        "inputs": {
            "soc_top_sim_path": soc_top_sim_path,
            "firmware_elf_path": firmware_elf_path,
            "makefile_path": makefile_path,
            "test_paths": test_paths,
            "coverage_model_path": coverage_model_path,
            "assertions_path": assertions_path,
            "rtl_inputs": rtl_inputs,
        },
        "test_matrix": test_matrix,
        "results": {
            "attempted": False,
            "executed_test_count": 0,
            "passed_test_count": 0,
            "failed_test_count": 0,
            "runtime_seconds_total": None,
            "waveform_paths": [],
            "log_paths": [
                "system/firmware/cosim/logs/system_firmware_execution.log"
            ],
        },
        "notes": _build_notes(readiness, optional_inputs),
    }

    dashboard = {
        "title": "System Firmware CoSim Dashboard",
        "generated_at": summary["generated_at"],
        "overall_status": overall_status,
        "ready_to_execute": readiness["status"] == "ready",
        "required_inputs_present": len(readiness["present"]),
        "required_inputs_missing": len(readiness["missing"]),
        "planned_test_count": len(test_matrix),
        "executed_test_count": 0,
        "passed_test_count": 0,
        "failed_test_count": 0,
        "firmware_elf_detected": bool(firmware_elf_path),
        "assertions_detected": bool(assertions_path),
        "coverage_model_detected": bool(coverage_model_path),
        "soc_top_module": _basename_no_ext(soc_top_sim_path) if soc_top_sim_path else None,
        "summary_path": "system/firmware/cosim/system_firmware_execution.md",
    }

    md = _markdown_report(summary)

    # Save canonical artifacts
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/cosim",
        "system_firmware_execution.json",
        json.dumps(summary, indent=2),
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/cosim",
        "system_firmware_execution.md",
        md,
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/cosim",
        "system_firmware_dashboard.json",
        json.dumps(dashboard, indent=2),
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "system/firmware/cosim/logs",
        "system_firmware_execution.log",
        "\n".join(
            [
                f"[{summary['generated_at']}] {agent_name}",
                f"overall_status={overall_status}",
                f"readiness={readiness['status']}",
                f"missing={', '.join(readiness['missing']) if readiness['missing'] else '(none)'}",
            ]
        )
        + "\n",
    )

    state["system_firmware_execution"] = summary
    state["system_firmware_dashboard"] = dashboard
    state["system_firmware_execution_path"] = "system/firmware/cosim/system_firmware_execution.json"
    state["system_firmware_execution_md_path"] = "system/firmware/cosim/system_firmware_execution.md"
    state["system_firmware_dashboard_path"] = "system/firmware/cosim/system_firmware_dashboard.json"
    state["status"] = (
        "✅ System firmware co-sim bundle is ready for execution"
        if readiness["status"] == "ready"
        else f"⚠️ System firmware co-sim blocked; missing: {', '.join(readiness['missing'])}"
    )
    return state