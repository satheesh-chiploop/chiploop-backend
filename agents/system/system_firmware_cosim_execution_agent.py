import datetime
import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Firmware CoSim Execution Agent"
OUTPUT_SUBDIR = "system/firmware/cosim"
LOG_SUBDIR = f"{OUTPUT_SUBDIR}/logs"
SUMMARY_JSON = "system_firmware_execution.json"
SUMMARY_MD = "system_firmware_execution.md"
DASHBOARD_JSON = "system_firmware_dashboard.json"
LOG_FILE = "system_firmware_execution.log"


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _norm_path(p: Any) -> str:
    return "" if p is None else str(p).strip().replace("\\", "/")


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _basename_no_ext(p: str) -> str:
    return os.path.splitext(os.path.basename(_norm_path(p)))[0]


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        norm = _norm_path(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _all_state_containers(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    containers = [state]
    for key in ("system", "embedded", "firmware", "firmware_build"):
        block = state.get(key)
        if isinstance(block, dict):
            containers.append(block)
    return containers


def _collect_candidate_values(state: Dict[str, Any], keys: List[str]) -> List[Any]:
    values: List[Any] = []
    for container in _all_state_containers(state):
        for key in keys:
            if key in container:
                values.append(container.get(key))
    return values


def _collect_paths_from_keys(state: Dict[str, Any], keys: List[str]) -> List[str]:
    found: List[str] = []
    for value in _collect_candidate_values(state, keys):
        if isinstance(value, list):
            found.extend([_norm_path(v) for v in value if _is_nonempty_str(v)])
        elif _is_nonempty_str(value):
            found.append(_norm_path(value))
    return _dedupe_keep_order(found)


def _first_path_from_keys(state: Dict[str, Any], keys: List[str]) -> str:
    hits = _collect_paths_from_keys(state, keys)
    return hits[0] if hits else ""


def _join_workflow_path(workflow_dir: str, rel_or_abs: str) -> str:
    if not rel_or_abs:
        return ""
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.abspath(os.path.join(workflow_dir, rel_or_abs))


def _existing_paths(workflow_dir: str, paths: List[str]) -> List[str]:
    out: List[str] = []
    for path in paths:
        abs_path = _join_workflow_path(workflow_dir, path)
        if abs_path and os.path.isfile(abs_path):
            out.append(path)
    return _dedupe_keep_order(out)


def _find_soc_top_path(state: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["soc_top_sim_path", "system_top_sim_path", "top_sim_path", "assembled_soc_top_path", "system_firmware_soc_top_path"])
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    for path in artifacts:
        low = path.lower()
        if low.endswith("_sim.sv") and "/system/" in low:
            return path
    return ""


def _find_elf_path(state: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["firmware_elf_path", "elf_path", "embedded_elf_path", "system_firmware_elf_path"])
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    for path in artifacts:
        if path.lower().endswith(".elf"):
            return path
    return ""


def _find_makefile_path(state: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["embedded_cocotb_makefile_path", "cocotb_makefile_path", "makefile_path", "digital_tb_makefile_path"])
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    for path in artifacts:
        if os.path.basename(path).lower() == "makefile":
            return path
    return ""


def _find_test_paths(state: Dict[str, Any]) -> List[str]:
    tests = _collect_paths_from_keys(state, ["embedded_cocotb_test_paths", "cocotb_test_paths", "test_paths", "digital_test_paths"])
    if tests:
        return tests
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    return _dedupe_keep_order([p for p in artifacts if os.path.basename(p).lower().startswith("test_") and p.lower().endswith(".py")])


def _find_coverage_model_path(state: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["coverage_model_path", "digital_coverage_model_path", "embedded_coverage_model_path"])
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    for path in artifacts:
        if os.path.basename(path).lower() == "coverage_model.py":
            return path
    return ""


def _find_assertions_path(state: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["assertions_path", "digital_assertions_path", "digital_sva_assertions_path"])
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(state, ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"])
    for path in artifacts:
        base = os.path.basename(path).lower()
        if base == "assertions.sv" or "assert" in base:
            return path
    return ""


def _find_verilog_inputs(state: Dict[str, Any], soc_top_path: str) -> List[str]:
    inputs = _collect_paths_from_keys(state, [
        "rtl_inputs", "system_rtl_files", "system_rtl_filelist_sim", "rtl_files", "verilog_files", "sv_files",
        "generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files",
    ])
    if soc_top_path:
        inputs.append(soc_top_path)
    return _dedupe_keep_order([p for p in inputs if p.lower().endswith((".sv", ".v"))])


def _readiness(required: Dict[str, Any]) -> Dict[str, Any]:
    missing: List[str] = []
    present: List[str] = []
    for name, value in required.items():
        ok = len(value) > 0 if isinstance(value, list) else bool(str(value).strip()) if value is not None else False
        (present if ok else missing).append(name)
    return {"status": "ready" if not missing else "blocked", "present": present, "missing": missing}


def _default_test_matrix(test_paths: List[str]) -> List[Dict[str, Any]]:
    matrix: List[Dict[str, Any]] = []
    for test_path in (test_paths or ["test_system_firmware_smoke.py"]):
        for seed in (1, 2):
            matrix.append({
                "test_name": os.path.basename(test_path),
                "seed": seed,
                "status": "planned",
                "runtime_seconds": None,
                "failure_reason": None,
            })
    return matrix


def _build_notes(readiness: Dict[str, Any], optional_inputs: Dict[str, Any], runtime_requested: bool, runtime_capable: bool) -> List[str]:
    notes: List[str] = []
    if readiness["status"] != "ready":
        notes.append("Execution was not attempted because one or more required inputs were missing.")
    elif runtime_requested and not runtime_capable:
        notes.append("Runtime execution was requested but the environment was not capable of executing the co-simulation bundle.")
    elif runtime_requested and runtime_capable:
        notes.append("Runtime execution was requested and attempted with the discovered cocotb collateral.")
    else:
        notes.append("Artifact readiness was evaluated without attempting runtime execution.")

    notes.append("Functional coverage model was detected and can be consumed by downstream summary." if optional_inputs.get("coverage_model") else "No coverage model detected; downstream summary should avoid reporting functional coverage percentages.")
    notes.append("Digital assertions collateral was detected and can be referenced by downstream summary." if optional_inputs.get("assertions") else "No digital assertions collateral detected; assertion coverage should remain unavailable, not fabricated.")
    notes.append("Firmware ELF was detected for firmware-aware co-simulation." if optional_inputs.get("elf_exists") else "Firmware ELF was not detected; this run should be treated as not executable, not as a passing simulation.")
    return notes


def _markdown_report(summary: Dict[str, Any]) -> str:
    readiness = summary.get("readiness", {})
    tests = summary.get("test_matrix", [])
    inputs = summary.get("inputs", {})
    notes = summary.get("notes", [])
    lines = [
        "# System Firmware CoSim Execution Summary", "",
        f"- Generated at: {summary.get('generated_at')}",
        f"- Execution mode: {summary.get('execution_mode')}",
        f"- Overall status: {summary.get('overall_status')}",
        f"- Readiness: {readiness.get('status')}", "",
        "## Key Inputs", "",
        f"- SoC top: `{inputs.get('soc_top_sim_path') or '(missing)'}`",
        f"- Firmware ELF: `{inputs.get('firmware_elf_path') or '(missing)'}`",
        f"- Cocotb Makefile: `{inputs.get('makefile_path') or '(missing)'}`",
        f"- Test files: `{len(inputs.get('test_paths') or [])}`",
        f"- Verilog/SystemVerilog files: `{len(inputs.get('rtl_inputs') or [])}`", "",
    ]
    if readiness.get("missing"):
        lines.extend(["## Missing Required Inputs", ""])
        lines.extend([f"- {m}" for m in readiness["missing"]])
        lines.append("")
    lines.extend(["## Planned Test Matrix", ""])
    lines.extend([f"- {t.get('test_name')} | seed={t.get('seed')} | status={t.get('status')}" for t in tests] or ["- No tests discovered."])
    lines.extend(["", "## Notes", ""])
    lines.extend([f"- {n}" for n in notes])
    lines.extend(["", "## Conclusion", ""])
    if summary.get("overall_status") == "ready_for_execution":
        lines.append("All required execution inputs were detected. This artifact indicates the co-simulation bundle is ready for execution.")
    elif summary.get("overall_status") == "simulation_passed":
        lines.append("Runtime execution completed successfully with the discovered co-simulation collateral.")
    elif summary.get("overall_status") == "simulation_failed":
        lines.append("Runtime execution was attempted but failed. Inspect the execution log and stderr tail for root cause.")
    else:
        lines.append("The co-simulation bundle is incomplete. Downstream agents should treat coverage as unavailable rather than infer passing results.")
    lines.append("")
    return "\n".join(lines)


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}



def _run_cocotb_simulation(workflow_dir: str, makefile_path: str, test_module: str) -> Dict[str, Any]:
    make_abs = _join_workflow_path(workflow_dir, makefile_path)
    if not os.path.isfile(make_abs):
        return {"attempted": False, "reason": "Makefile not found"}
    make_bin = shutil.which("make")
    if not make_bin:
        return {"attempted": False, "reason": "make not available"}
    start = time.time()
    try:
        proc = subprocess.run(
            [make_bin, "-f", make_abs, f"MODULE={test_module}"],
            cwd=workflow_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "attempted": True,
            "success": proc.returncode == 0,
            "runtime_seconds": time.time() - start,
            "stdout": (proc.stdout or "")[-5000:],
            "stderr": (proc.stderr or "")[-5000:],
        }
    except Exception as exc:
        return {"attempted": True, "success": False, "runtime_seconds": None, "stderr": str(exc)}


def _elf_is_placeholder(state: Dict[str, Any], workflow_dir: str, firmware_elf_path: str) -> bool:
    build_block = state.get("firmware_build") or {}
    if build_block.get("build_succeeded") is False and state.get("firmware_elf_exists"):
        return True

    debug_candidates = [
        os.path.join(workflow_dir, "firmware", "debug", "elf_build_result.json"),
        _join_workflow_path(workflow_dir, "firmware/debug/elf_build_result.json"),
    ]
    for dbg in debug_candidates:
        obj = _safe_read_json(dbg)
        if obj:
            if obj.get("build_succeeded") is False and obj.get("elf_exists") is True:
                return True
            stderr_tail = str(obj.get("stderr_tail") or "")
            if "Cargo not found in PATH" in stderr_tail and obj.get("elf_exists") is True:
                return True

    abs_elf = _join_workflow_path(workflow_dir, firmware_elf_path)
    try:
        if abs_elf and os.path.isfile(abs_elf):
            with open(abs_elf, "rb") as f:
                head = f.read(64)
            if b"ELF_PLACEHOLDER_BINARY" in head:
                return True
    except Exception:
        pass
    return False

def _find_coverage_model_path(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(
        state,
        ["coverage_model_path", "digital_coverage_model_path", "embedded_coverage_model_path"],
    )
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(
        state,
        ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"],
    )
    for path in artifacts:
        if os.path.basename(path).lower() == "coverage_model.py":
            return path
    for rel in ("vv/tb/coverage_model.py", "system/firmware/coverage/coverage_model.py"):
        if os.path.isfile(_join_workflow_path(workflow_dir, rel)):
            return rel
    return ""

def _find_assertions_path(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(
        state,
        ["assertions_path", "digital_assertions_path", "digital_sva_assertions_path"],
    )
    if direct:
        return direct
    artifacts = _collect_paths_from_keys(
        state,
        ["generated_files", "artifacts", "artifact_paths", "output_files", "produced_artifacts", "files"],
    )
    for path in artifacts:
        base = os.path.basename(path).lower()
        if base == "assertions.sv" or "assert" in base:
            return path
    for rel in ("vv/tb/assertions.sv", "vv/tb/soc_top_sim_assertions.sv", "vv/tb/soc_top_assertions.sv"):
        if os.path.isfile(_join_workflow_path(workflow_dir, rel)):
            return rel
    return ""


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    print("\n⚙️ Running System Firmware CoSim Execution Agent")

    workflow_dir = state.get("workflow_dir")
    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for cosim execution"
        return state

    soc_top_sim_path = _find_soc_top_path(state)
    firmware_elf_path = _find_elf_path(state)
    makefile_path = _find_makefile_path(state)
    test_paths = _find_test_paths(state)
    coverage_model_path = _find_coverage_model_path(state, workflow_dir)
    assertions_path = _find_assertions_path(state, workflow_dir)
    rtl_inputs = _find_verilog_inputs(state, soc_top_sim_path)

    soc_top_exists = bool(soc_top_sim_path and os.path.isfile(_join_workflow_path(workflow_dir, soc_top_sim_path)))
    rtl_inputs = _existing_paths(workflow_dir, rtl_inputs)


    # Normalize to relative paths for consistency
    normalized_rtl = []
    for p in rtl_inputs:
        abs_p = _join_workflow_path(workflow_dir, p)
        try:
            rel_p = os.path.relpath(abs_p, workflow_dir)
            normalized_rtl.append(rel_p.replace("\\", "/"))
        except Exception:
            normalized_rtl.append(p)

    rtl_inputs = _dedupe_keep_order(normalized_rtl)

    
    elf_abs = _join_workflow_path(workflow_dir, firmware_elf_path) if firmware_elf_path else ""
    elf_exists = bool(state.get("firmware_elf_exists") or (elf_abs and os.path.isfile(elf_abs)))
    elf_placeholder = bool(elf_exists and _elf_is_placeholder(state, workflow_dir, firmware_elf_path))
    real_elf_exists = bool(elf_exists and not elf_placeholder)

    required = {
        "soc_top_sim_path": soc_top_sim_path if soc_top_exists else "",
        "firmware_elf_real": "yes" if real_elf_exists else "",
        "makefile_path": makefile_path,
        "test_paths": test_paths,
        "rtl_inputs": rtl_inputs,
    }
    readiness = _readiness(required)
    runtime_requested = bool(state.get("execute_cosim") or state.get("run_cosim"))
    runtime_capable = bool(makefile_path and elf_exists and soc_top_exists and test_paths)

    optional_inputs = {
        "coverage_model": coverage_model_path,
        "assertions": assertions_path,
        "elf_exists": elf_exists,
    }

    sim_result: Optional[Dict[str, Any]] = None
    if readiness["status"] != "ready":
        execution_mode = "artifact_readiness_only"
        overall_status = "ready_with_placeholder_elf" if elf_placeholder else "blocked_missing_inputs"
    elif runtime_requested and runtime_capable:
        execution_mode = "runtime_execution"
        test_module = os.path.splitext(os.path.basename(test_paths[0]))[0]
        sim_result = _run_cocotb_simulation(workflow_dir, makefile_path, test_module)
        overall_status = "simulation_passed" if sim_result.get("success") else "simulation_failed"
    else:
        execution_mode = "artifact_readiness_only"
        overall_status = "ready_for_execution"

    test_matrix = _default_test_matrix(test_paths)
    if sim_result and sim_result.get("attempted"):
        for row in test_matrix:
            if row["test_name"] == os.path.basename(test_paths[0]):
                row["status"] = "passed" if sim_result.get("success") else "failed"
                row["runtime_seconds"] = sim_result.get("runtime_seconds")
                row["failure_reason"] = None if sim_result.get("success") else (sim_result.get("stderr") or "simulation_failed")
                break

    summary = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "execution_mode": execution_mode,
        "overall_status": overall_status,
        "readiness": readiness,
        "inputs": {
            "soc_top_sim_path": soc_top_sim_path if soc_top_exists else "",
            "firmware_elf_path": firmware_elf_path,
            "firmware_elf_exists": elf_exists,
            "firmware_elf_placeholder": elf_placeholder,
            "makefile_path": makefile_path,
            "test_paths": test_paths,
            "coverage_model_path": coverage_model_path,
            "assertions_path": assertions_path,
            "rtl_inputs": rtl_inputs,
        },
        "test_matrix": test_matrix,
        "results": {
            "attempted": bool(sim_result),
            "executed_test_count": 1 if sim_result else 0,
            "passed_test_count": 1 if sim_result and sim_result.get("success") else 0,
            "failed_test_count": 1 if sim_result and not sim_result.get("success") else 0,
            "runtime_seconds_total": sim_result.get("runtime_seconds") if sim_result else None,
            "waveform_paths": [],
            "log_paths": [f"{LOG_SUBDIR}/{LOG_FILE}"],
            "runtime_requested": runtime_requested,
            "runtime_capable": runtime_capable,
            "stdout_tail": sim_result.get("stdout") if sim_result else "",
            "stderr_tail": sim_result.get("stderr") if sim_result else "",
        },
        "elf_detection_source": "state_flag" if state.get("firmware_elf_exists") else "filesystem",
        "notes": _build_notes(readiness, optional_inputs, runtime_requested, runtime_capable),
    }

    dashboard = {
        "title": "System Firmware CoSim Dashboard",
        "generated_at": summary["generated_at"],
        "overall_status": overall_status,
        "ready_to_execute": readiness["status"] == "ready",
        "required_inputs_present": len(readiness["present"]),
        "required_inputs_missing": len(readiness["missing"]),
        "planned_test_count": len(test_matrix),
        "executed_test_count": summary["results"]["executed_test_count"],
        "passed_test_count": summary["results"]["passed_test_count"],
        "failed_test_count": summary["results"]["failed_test_count"],
        "firmware_elf_detected": elf_exists,
        "assertions_detected": bool(assertions_path),
        "coverage_model_detected": bool(coverage_model_path),
        "soc_top_module": _basename_no_ext(soc_top_sim_path) if soc_top_sim_path else None,
        "summary_path": f"{OUTPUT_SUBDIR}/{SUMMARY_MD}",
    }

    md = _markdown_report(summary)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, SUMMARY_JSON, json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, SUMMARY_MD, md)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, OUTPUT_SUBDIR, DASHBOARD_JSON, json.dumps(dashboard, indent=2))
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        LOG_SUBDIR,
        LOG_FILE,
        "\n".join([
            f"[{summary['generated_at']}] {AGENT_NAME}",
            f"overall_status={overall_status}",
            f"readiness={readiness['status']}",
            f"missing={', '.join(readiness['missing']) if readiness['missing'] else '(none)'}",
        ]) + "\n",
    )

    state["system_firmware_execution"] = summary
    state["system_firmware_dashboard"] = dashboard
    state["system_firmware_execution_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_JSON}"
    state["system_firmware_execution_md_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"
    state["system_firmware_dashboard_path"] = f"{OUTPUT_SUBDIR}/{DASHBOARD_JSON}"
    state["status"] = (
        "✅ System firmware co-sim simulation passed"
        if overall_status == "simulation_passed"
        else "✅ System firmware co-sim bundle is ready for execution"
        if readiness["status"] == "ready"
        else f"⚠️ System firmware co-sim blocked; missing: {', '.join(readiness['missing'])}"
    )
    return state
