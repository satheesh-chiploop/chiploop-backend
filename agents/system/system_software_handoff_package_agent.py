import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software Handoff Package Agent"
OUTPUT_SUBDIR = "system/software_handoff"
MANIFEST_JSON = "system_software_handoff.json"
SUMMARY_MD = "system_software_handoff.md"
FILELIST_TXT = "software_artifact_filelist.txt"
DEBUG_JSON = "system_software_handoff_debug.json"


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _path_exists(workflow_dir: str, rel_or_abs: str) -> bool:
    abs_path = _join_workflow_path(workflow_dir, rel_or_abs)
    return bool(abs_path and os.path.exists(abs_path))


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _safe_read_text(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _record_text(workflow_id: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _find_firmware_manifest_path(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(state, ["firmware_manifest_path", "manifest_path"])
    if direct:
        return direct
    for rel in ("firmware/firmware_manifest.json",):
        if _path_exists(workflow_dir, rel):
            return rel
    return ""


def _find_register_map_path(state: Dict[str, Any], workflow_dir: str, manifest: Dict[str, Any]) -> str:
    direct = _first_path_from_keys(state, ["firmware_register_map_path", "register_map_path", "regmap_json"])
    if direct:
        return direct
    manifest_candidate = _norm_path(manifest.get("register_map_path"))
    if manifest_candidate:
        return manifest_candidate
    for rel in ("firmware/register_map.json", "digital/digital_regmap.json", "handoff/docs/regmap/digital_regmap.json"):
        if _path_exists(workflow_dir, rel):
            return rel
    return ""


def _find_system_integration_intent_path(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(state, ["system_integration_intent_json", "integration_json_path"])
    if direct:
        return direct
    for rel in ("system/integration/system_integration_intent.json", "system_integration_intent.json"):
        if _path_exists(workflow_dir, rel):
            return rel
    return ""


def _find_top_module(state: Dict[str, Any], integration_obj: Dict[str, Any]) -> str:
    for key in ("soc_top_sim_module", "system_top_sim_module", "top_module", "soc_top_name"):
        vals = _collect_candidate_values(state, [key])
        for v in vals:
            if _is_nonempty_str(v):
                return _norm_path(v)
    top = integration_obj.get("top") if isinstance(integration_obj.get("top"), dict) else {}
    for key in ("sim_module", "base_name"):
        v = top.get(key)
        if _is_nonempty_str(v):
            return _norm_path(v)
    return ""


def _find_soc_top_sim_path(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(state, ["soc_top_sim_path", "system_top_sim_path", "top_sim_path"])
    if direct:
        return direct
    if _path_exists(workflow_dir, "system/integration/soc_top_sim.sv"):
        return "system/integration/soc_top_sim.sv"
    integ_dir = os.path.join(workflow_dir, "system", "integration")
    if os.path.isdir(integ_dir):
        for name in sorted(os.listdir(integ_dir)):
            if name.endswith("_sim.sv"):
                return f"system/integration/{name}"
    return ""


def _find_rtl_filelist(state: Dict[str, Any], workflow_dir: str) -> Tuple[str, List[str]]:
    list_from_state = []
    for key in ("system_rtl_filelist_sim", "rtl_inputs", "system_rtl_files", "rtl_filelist_sim"):
        values = _collect_candidate_values(state, [key])
        for v in values:
            if isinstance(v, list):
                cleaned = [_norm_path(x) for x in v if _is_nonempty_str(x)]
                if cleaned:
                    list_from_state.extend(cleaned)
    list_from_state = _dedupe_keep_order(list_from_state)
    if list_from_state:
        return "", list_from_state

    direct = _first_path_from_keys(state, ["system_filelist_sim_path", "rtl_filelist_path", "filelist_path"])
    if direct:
        abs_p = _join_workflow_path(workflow_dir, direct)
        if os.path.isfile(abs_p):
            lines = [ln.strip().replace("\\", "/") for ln in _safe_read_text(abs_p).splitlines() if ln.strip()]
            return direct, _dedupe_keep_order(lines)

    for rel in ("system/integration/system_rtl_filelist_sim.txt", "firmware/validate/verilator_rtl_filelist.f"):
        if _path_exists(workflow_dir, rel):
            lines = [ln.strip().replace("\\", "/") for ln in _safe_read_text(_join_workflow_path(workflow_dir, rel)).splitlines() if ln.strip()]
            return rel, _dedupe_keep_order(lines)
    return "", []


def _find_elf_info(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    elf_path = _first_path_from_keys(state, ["firmware_elf_path", "elf_path", "embedded_elf_path", "system_firmware_elf_path"])
    elf_exists_flag = bool(state.get("firmware_elf_exists"))
    elf_abs = _join_workflow_path(workflow_dir, elf_path) if elf_path else ""
    elf_exists_fs = bool(elf_abs and os.path.isfile(elf_abs))
    elf_exists = bool(elf_exists_flag or elf_exists_fs)

    build_block = state.get("firmware_build") if isinstance(state.get("firmware_build"), dict) else {}
    placeholder = False
    if build_block and build_block.get("build_succeeded") is False and elf_exists:
        placeholder = True

    dbg = _safe_read_json(_join_workflow_path(workflow_dir, "firmware/debug/elf_build_result.json"))
    if dbg:
        stderr_tail = str(dbg.get("stderr_tail") or "")
        if dbg.get("build_succeeded") is False and dbg.get("elf_exists") is True:
            placeholder = True
        if "Cargo not found in PATH" in stderr_tail and dbg.get("elf_exists") is True:
            placeholder = True

    try:
        if elf_abs and os.path.isfile(elf_abs):
            with open(elf_abs, "rb") as f:
                head = f.read(64)
            if b"ELF_PLACEHOLDER_BINARY" in head:
                placeholder = True
    except Exception:
        pass

    return {
        "elf_path": elf_path,
        "elf_exists": elf_exists,
        "elf_placeholder": placeholder,
        "build_attempted": build_block.get("build_attempted"),
        "build_succeeded": build_block.get("build_succeeded"),
        "target_triple": build_block.get("target_triple"),
        "bin_name": build_block.get("bin_name"),
    }


def _find_cocotb_paths(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    makefile_path = _first_path_from_keys(state, ["embedded_cocotb_makefile_path", "cocotb_makefile_path", "makefile_path"])
    if not makefile_path and _path_exists(workflow_dir, "firmware/validate/Makefile"):
        makefile_path = "firmware/validate/Makefile"

    test_paths = _collect_paths_from_keys(state, ["embedded_cocotb_test_paths", "cocotb_test_paths", "test_paths"])
    if not test_paths:
        test_dir = os.path.join(workflow_dir, "firmware", "validate")
        if os.path.isdir(test_dir):
            test_paths = [f"firmware/validate/{n}" for n in sorted(os.listdir(test_dir)) if n.startswith("test_") and n.endswith(".py")]

    harness_path = _first_path_from_keys(state, ["cocotb_harness_path", "sim_harness_path", "expected_cocotb_harness_path"])
    if not harness_path and _path_exists(workflow_dir, "firmware/validate/cocotb_harness.py"):
        harness_path = "firmware/validate/cocotb_harness.py"

    return {
        "makefile_path": makefile_path,
        "test_paths": _dedupe_keep_order(test_paths),
        "harness_path": harness_path,
    }


def _find_execution_summary(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_firmware_execution") if isinstance(state.get("system_firmware_execution"), dict) else {}
    if obj:
        return obj
    abs_p = _join_workflow_path(workflow_dir, "system/firmware/cosim/system_firmware_execution.json")
    return _safe_read_json(abs_p) if os.path.isfile(abs_p) else {}


def _find_coverage_summary(state: Dict[str, Any], workflow_dir: str) -> Dict[str, Any]:
    obj = state.get("system_firmware_coverage_summary") if isinstance(state.get("system_firmware_coverage_summary"), dict) else {}
    if obj:
        return obj
    for rel in ("system/firmware/coverage/system_firmware_coverage_summary.json", "coverage/coverage_summary.json"):
        abs_p = _join_workflow_path(workflow_dir, rel)
        if os.path.isfile(abs_p):
            return _safe_read_json(abs_p)
    return {}


def _find_validation_report(state: Dict[str, Any], workflow_dir: str) -> str:
    direct = _first_path_from_keys(state, ["firmware_validation_report_path", "validation_report_path"])
    if direct:
        return direct
    if _path_exists(workflow_dir, "firmware/validate/validation_report.md"):
        return "firmware/validate/validation_report.md"
    return ""


def _find_public_api_candidates(workflow_dir: str) -> List[str]:
    candidates: List[str] = []
    scan_roots = [os.path.join(workflow_dir, "firmware"), os.path.join(workflow_dir, "system")]
    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        for walk_root, _, files in os.walk(root):
            for name in sorted(files):
                low = name.lower()
                if low.endswith((".h", ".hpp", ".rs", ".json", ".md")):
                    rel = os.path.relpath(os.path.join(walk_root, name), workflow_dir).replace("\\", "/")
                    if any(token in rel.lower() for token in ("driver", "register", "hal", "handoff", "manifest", "software_handoff")):
                        candidates.append(rel)
    return _dedupe_keep_order(candidates)


def _build_manifest(state, workflow_dir, firmware_manifest_path, firmware_manifest, register_map_path,
                    system_integration_intent_path, top_module, soc_top_sim_path, rtl_filelist_path,
                    rtl_file_entries, elf_info, cocotb_info, execution_summary, coverage_summary,
                    validation_report_path, api_candidates):
    exec_readiness = (execution_summary.get("readiness") or {}) if isinstance(execution_summary, dict) else {}
    cov_metrics = (coverage_summary.get("coverage_metrics") or {}) if isinstance(coverage_summary, dict) else {}
    pkg = {
        "package_type": "system_software_handoff",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": state.get("workflow_id"),
        "source_workflow_type": "System_Firmware",
        "system_contract": {
            "top_module": top_module,
            "soc_top_sim_path": soc_top_sim_path,
            "system_integration_intent_path": system_integration_intent_path,
            "rtl_filelist_path": rtl_filelist_path,
            "rtl_file_entries": rtl_file_entries,
        },
        "firmware_contract": {
            "firmware_manifest_path": firmware_manifest_path,
            "register_map_path": register_map_path,
            "hal_path": _norm_path(firmware_manifest.get("hal_path")),
            "driver_path": _norm_path(firmware_manifest.get("driver_path")),
            "register_dump_path": _norm_path(firmware_manifest.get("register_dump_path")),
            "elf_path": elf_info.get("elf_path"),
            "elf_exists": elf_info.get("elf_exists"),
            "elf_placeholder": elf_info.get("elf_placeholder"),
            "build_attempted": elf_info.get("build_attempted"),
            "build_succeeded": elf_info.get("build_succeeded"),
            "target_triple": elf_info.get("target_triple"),
            "bin_name": elf_info.get("bin_name"),
        },
        "verification_contract": {
            "cocotb_makefile_path": cocotb_info.get("makefile_path"),
            "cocotb_test_paths": cocotb_info.get("test_paths"),
            "cocotb_harness_path": cocotb_info.get("harness_path"),
            "execution_overall_status": execution_summary.get("overall_status"),
            "execution_readiness": exec_readiness.get("status"),
            "coverage_available": cov_metrics.get("coverage_available"),
            "validation_report_path": validation_report_path,
        },
        "software_readiness": {
            "ready_for_system_software": True,
            "package_quality": "provisional" if elf_info.get("elf_placeholder") else "ready",
            "blocking_gaps": [],
            "assumptions": [],
        },
        "developer_api_candidates": api_candidates,
    }
    gaps = []
    assumptions = []
    if not register_map_path:
        gaps.append("register_map_missing")
    if not soc_top_sim_path:
        gaps.append("soc_top_sim_missing")
    if not rtl_file_entries:
        gaps.append("rtl_filelist_missing")
    if not elf_info.get("elf_exists"):
        gaps.append("firmware_elf_missing")
    if elf_info.get("elf_placeholder"):
        assumptions.append("firmware_elf_is_placeholder")
    if not cocotb_info.get("makefile_path"):
        assumptions.append("cocotb_makefile_missing")
    if exec_readiness.get("status") == "blocked":
        assumptions.append("system_firmware_execution_blocked")
    pkg["software_readiness"]["blocking_gaps"] = gaps
    pkg["software_readiness"]["assumptions"] = assumptions
    return pkg


def _markdown_report(manifest: Dict[str, Any]) -> str:
    sysc = manifest.get("system_contract") or {}
    fwc = manifest.get("firmware_contract") or {}
    verc = manifest.get("verification_contract") or {}
    rd = manifest.get("software_readiness") or {}
    lines = [
        "# System Software Handoff Package",
        "",
        f"- Generated at: {manifest.get('generated_at')}",
        f"- Source workflow id: {manifest.get('source_workflow_id')}",
        f"- Package version: {manifest.get('package_version')}",
        "",
        "## Overview",
        "",
        f"- Top module: `{sysc.get('top_module') or 'unavailable'}`",
        f"- SoC sim top path: `{sysc.get('soc_top_sim_path') or 'unavailable'}`",
        f"- RTL file count: `{len(sysc.get('rtl_file_entries') or [])}`",
        f"- Firmware ELF path: `{fwc.get('elf_path') or 'unavailable'}`",
        f"- Firmware ELF exists: `{fwc.get('elf_exists')}`",
        f"- Firmware ELF placeholder: `{fwc.get('elf_placeholder')}`",
        f"- System firmware execution readiness: `{verc.get('execution_readiness') or 'unavailable'}`",
        f"- Coverage available: `{verc.get('coverage_available')}`",
        "",
        "## Artifacts",
        "",
    ]
    artifact_paths = [
        sysc.get("system_integration_intent_path"),
        sysc.get("soc_top_sim_path"),
        sysc.get("rtl_filelist_path"),
        fwc.get("firmware_manifest_path"),
        fwc.get("register_map_path"),
        fwc.get("hal_path"),
        fwc.get("driver_path"),
        fwc.get("register_dump_path"),
        fwc.get("elf_path"),
        verc.get("cocotb_makefile_path"),
        *(verc.get("cocotb_test_paths") or []),
        verc.get("cocotb_harness_path"),
        verc.get("validation_report_path"),
        *(manifest.get("developer_api_candidates") or []),
    ]
    for path in _dedupe_keep_order([p for p in artifact_paths if _is_nonempty_str(p)]):
        lines.append(f"- `{path}`")
    lines.extend(["", "## Key assumptions", ""])
    assumptions = rd.get("assumptions") or []
    if assumptions:
        lines.extend([f"- {a}" for a in assumptions])
    else:
        lines.append("- (none)")
    lines.extend(["", "## Risks / Gaps", ""])
    gaps = rd.get("blocking_gaps") or []
    if gaps:
        lines.extend([f"- {g}" for g in gaps])
    else:
        lines.append("- (none recorded)")
    lines.extend([
        "",
        "## Next system software steps",
        "",
        "- Consume `system_software_handoff.json` as the primary machine-readable input.",
        "- Build the public system software package against the register map, HAL, and driver contract rather than scraping raw artifacts.",
        "- Treat placeholder ELF as non-executable for runtime validation even if the file path exists.",
        "- Use the runtime/simulation contract only for validation stages, not as part of the software build itself.",
        "",
    ])
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir")
    print(f"\n📦 Running {AGENT_NAME}")
    if not workflow_dir:
        state["status"] = "❌ workflow_dir missing for system software handoff"
        return state

    firmware_manifest_path = _find_firmware_manifest_path(state, workflow_dir)
    firmware_manifest = _safe_read_json(_join_workflow_path(workflow_dir, firmware_manifest_path)) if firmware_manifest_path else {}
    register_map_path = _find_register_map_path(state, workflow_dir, firmware_manifest)
    system_integration_intent_path = _find_system_integration_intent_path(state, workflow_dir)
    integration_obj = _safe_read_json(_join_workflow_path(workflow_dir, system_integration_intent_path)) if system_integration_intent_path else {}
    top_module = _find_top_module(state, integration_obj)
    soc_top_sim_path = _find_soc_top_sim_path(state, workflow_dir)
    rtl_filelist_path, rtl_file_entries = _find_rtl_filelist(state, workflow_dir)
    elf_info = _find_elf_info(state, workflow_dir)
    cocotb_info = _find_cocotb_paths(state, workflow_dir)
    execution_summary = _find_execution_summary(state, workflow_dir)
    coverage_summary = _find_coverage_summary(state, workflow_dir)
    validation_report_path = _find_validation_report(state, workflow_dir)
    api_candidates = _find_public_api_candidates(workflow_dir)

    manifest = _build_manifest(
        state, workflow_dir, firmware_manifest_path, firmware_manifest, register_map_path,
        system_integration_intent_path, top_module, soc_top_sim_path, rtl_filelist_path,
        rtl_file_entries, elf_info, cocotb_info, execution_summary, coverage_summary,
        validation_report_path, api_candidates
    )

    filelist_entries = _dedupe_keep_order([
        manifest.get("system_contract", {}).get("system_integration_intent_path"),
        manifest.get("system_contract", {}).get("soc_top_sim_path"),
        manifest.get("system_contract", {}).get("rtl_filelist_path"),
        manifest.get("firmware_contract", {}).get("firmware_manifest_path"),
        manifest.get("firmware_contract", {}).get("register_map_path"),
        manifest.get("firmware_contract", {}).get("hal_path"),
        manifest.get("firmware_contract", {}).get("driver_path"),
        manifest.get("firmware_contract", {}).get("register_dump_path"),
        manifest.get("firmware_contract", {}).get("elf_path"),
        manifest.get("verification_contract", {}).get("cocotb_makefile_path"),
        *(manifest.get("verification_contract", {}).get("cocotb_test_paths") or []),
        manifest.get("verification_contract", {}).get("cocotb_harness_path"),
        manifest.get("verification_contract", {}).get("validation_report_path"),
        *api_candidates,
    ])

    summary_md = _markdown_report(manifest)
    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "source_workflow_id": workflow_id,
        "observed_paths": filelist_entries,
        "elf_info": elf_info,
        "execution_summary_present": bool(execution_summary),
        "coverage_summary_present": bool(coverage_summary),
    }

    _record_text(workflow_id, OUTPUT_SUBDIR, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, OUTPUT_SUBDIR, SUMMARY_MD, summary_md)
    _record_text(workflow_id, OUTPUT_SUBDIR, FILELIST_TXT, "\n".join(filelist_entries) + ("\n" if filelist_entries else ""))
    _record_text(workflow_id, OUTPUT_SUBDIR, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_handoff"] = manifest
    state["system_software_handoff_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["system_software_handoff_md_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"
    state["system_software_handoff_filelist_path"] = f"{OUTPUT_SUBDIR}/{FILELIST_TXT}"

    system_block = state.setdefault("system", {})
    system_block["software_handoff"] = manifest
    system_block["software_handoff_path"] = state["system_software_handoff_path"]

    state["status"] = "✅ System software handoff package generated"
    return state
