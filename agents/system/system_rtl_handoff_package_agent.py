import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System RTL Handoff Package Agent"
OUTPUT_SUBDIR = "system/package"

PACKAGE_JSON = "system_rtl_package.json"
SUMMARY_MD = "system_rtl_package_summary.md"
DEBUG_JSON = "system_rtl_package_debug.json"

ARTIFACT_BUCKET_CANDIDATES = ["artifacts"]

SYSTEM_INTEGRATION_INTENT_SUFFIXES = [
    "system/integration/system_integration_intent.json",
]

SOC_TOP_SIM_SUFFIXES = [
    "system/integration/soc_top_sim.sv",
]

SOC_TOP_PHYS_SUFFIXES = [
    "system/integration/soc_top_phys.sv",
]

RTL_FILELIST_SIM_SUFFIXES = [
    "system/integration/system_rtl_filelist_sim.txt",
]

RTL_FILELIST_PHYS_SUFFIXES = [
    "system/integration/system_rtl_filelist_phys.txt",
]

LIB_FILELIST_PHYS_SUFFIXES = [
    "system/integration/system_lib_filelist_phys.txt",
]

FULL_COMPILE_SUMMARY_SUFFIXES = [
    "system/integration/system_full_compile_summary.json",
]

DISCOVERED_RTL_SUFFIXES = [
    "system/integration/system_discovered_rtl.txt",
]

DISCOVERED_LIBS_SUFFIXES = [
    "system/integration/system_discovered_libs.txt",
]

PASS1_SIM_SUFFIXES = [
    "system/integration/soc_top_sim_pass1.sv",
]

PASS1_PHYS_SUFFIXES = [
    "system/integration/soc_top_phys_pass1.sv",
]

IVERILOG_SIM_LOG_SUFFIXES = [
    "system/integration/system_full_compile_iverilog_sim_pass1.log",
]

VERILATOR_SIM_LOG_SUFFIXES = [
    "system/integration/system_full_compile_verilator_sim_pass1.log",
]

IVERILOG_PHYS_LOG_SUFFIXES = [
    "system/integration/system_full_compile_iverilog_phys_pass1.log",
]

VERILATOR_PHYS_LOG_SUFFIXES = [
    "system/integration/system_full_compile_verilator_phys_pass1.log",
]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


def _safe_json_loads(text: Any) -> Dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


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
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _join_workflow_path(workflow_dir: str, rel_or_abs: str) -> str:
    if not rel_or_abs:
        return ""
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.abspath(os.path.join(workflow_dir, rel_or_abs))


def _record_text(workflow_id: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _get_supabase(state: Dict[str, Any]):
    client = state.get("supabase_client")
    if client is None:
        raise RuntimeError("supabase_client missing from state")
    return client


def _get_source_workflow_id(state: Dict[str, Any]) -> str:
    for key in (
        "system_rtl_workflow_id",
        "rtl_workflow_id",
        "source_rtl_workflow_id",
        "source_workflow_id",
    ):
        value = state.get(key)
        if _is_nonempty_str(value):
            return str(value).strip()

    system_block = state.get("system") or {}
    if isinstance(system_block, dict):
        for key in ("rtl_workflow_id", "source_rtl_workflow_id"):
            value = system_block.get(key)
            if _is_nonempty_str(value):
                return str(value).strip()

    return ""


def _workflow_row(supabase, workflow_id: str) -> Dict[str, Any]:
    if not workflow_id:
        return {}
    try:
        resp = (
            supabase.table("workflows")
            .select("id,user_id,name,loop_type,artifacts,definitions")
            .eq("id", workflow_id)
            .single()
            .execute()
        )
        return resp.data or {}
    except Exception:
        return {}


def _workflow_storage_prefixes(state: Dict[str, Any], source_workflow_id: str, workflow_row: Dict[str, Any]) -> List[str]:
    prefixes: List[str] = []
    user_id = (workflow_row or {}).get("user_id")
    loop_type = (workflow_row or {}).get("loop_type") or "system"
    app_name = state.get("app_name") or "system_rtl_package"

    explicit_prefix = state.get("source_artifact_prefix")
    if _is_nonempty_str(explicit_prefix):
        prefixes.append(_norm_path(explicit_prefix).rstrip("/") + "/")

    prefixes.append(f"backend/workflows/{source_workflow_id}/")

    if _is_nonempty_str(user_id):
        prefixes.append(f"{user_id}/{source_workflow_id}/")
        prefixes.append(f"artifacts/{user_id}/{source_workflow_id}/")
        prefixes.append(f"backend/workflows/{source_workflow_id}/{loop_type}/{app_name}/")

    seen = set()
    out: List[str] = []
    for prefix in prefixes:
        norm = _norm_path(prefix)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _storage_download_json(supabase, bucket: str, path: str) -> Dict[str, Any]:
    try:
        data = supabase.storage.from_(bucket).download(path)
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        return _safe_json_loads(text)
    except Exception:
        return {}


def _storage_download_text(supabase, bucket: str, path: str) -> str:
    try:
        data = supabase.storage.from_(bucket).download(path)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)
    except Exception:
        return ""


def _storage_download_bytes(supabase, bucket: str, path: str) -> bytes:
    try:
        data = supabase.storage.from_(bucket).download(path)
        return data if isinstance(data, bytes) else str(data).encode("utf-8")
    except Exception:
        return b""


def _candidate_values(state: Dict[str, Any], explicit_keys: List[str], suffix_candidates: List[str]) -> List[str]:
    values: List[str] = []

    for key in explicit_keys:
        value = state.get(key)
        if _is_nonempty_str(value):
            values.append(_norm_path(value))

    system_block = state.get("system")
    if isinstance(system_block, dict):
        for key in explicit_keys:
            value = system_block.get(key)
            if _is_nonempty_str(value):
                values.append(_norm_path(value))

    values.extend(suffix_candidates)

    seen = set()
    out: List[str] = []
    for item in values:
        item = _norm_path(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _storage_try_candidates(prefixes: List[str], target: str) -> List[str]:
    candidate_paths: List[str] = []
    if target.startswith("backend/workflows/") or target.startswith("artifacts/"):
        candidate_paths.append(target)
    else:
        for prefix in prefixes:
            candidate_paths.append(f"{prefix.rstrip('/')}/{target}")
        candidate_paths.append(target)

    seen = set()
    ordered: List[str] = []
    for p in candidate_paths:
        p = _norm_path(p)
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _resolve_json_asset(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    explicit_state_key: str,
    path_keys: List[str],
    suffix_candidates: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    obj = state.get(explicit_state_key)
    if isinstance(obj, dict) and obj:
        return obj, {"mode": "state", "resolved_path": f"state:{explicit_state_key}", "bucket": ""}

    if source_workflow_id:
        supabase = _get_supabase(state)
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            for bucket in ARTIFACT_BUCKET_CANDIDATES:
                for full_path in _storage_try_candidates(prefixes, candidate):
                    found_obj = _storage_download_json(supabase, bucket, full_path)
                    if found_obj:
                        return found_obj, {"mode": "supabase", "resolved_path": full_path, "bucket": bucket}

    if workflow_dir:
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            local_path = _join_workflow_path(workflow_dir, candidate)
            found_obj = _safe_read_json(local_path)
            if found_obj:
                return found_obj, {"mode": "local", "resolved_path": local_path, "bucket": ""}

    return {}, {"mode": "missing", "resolved_path": "", "bucket": ""}


def _resolve_text_asset(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    explicit_state_key: str,
    path_keys: List[str],
    suffix_candidates: List[str],
) -> Tuple[str, Dict[str, Any]]:
    obj = state.get(explicit_state_key)
    if _is_nonempty_str(obj):
        return str(obj), {"mode": "state", "resolved_path": f"state:{explicit_state_key}", "bucket": ""}

    if source_workflow_id:
        supabase = _get_supabase(state)
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            for bucket in ARTIFACT_BUCKET_CANDIDATES:
                for full_path in _storage_try_candidates(prefixes, candidate):
                    found_text = _storage_download_text(supabase, bucket, full_path)
                    if found_text:
                        return found_text, {"mode": "supabase", "resolved_path": full_path, "bucket": bucket}

    if workflow_dir:
        candidates = _candidate_values(state, path_keys, suffix_candidates)
        for candidate in candidates:
            local_path = _join_workflow_path(workflow_dir, candidate)
            found_text = _safe_read_text(local_path)
            if found_text:
                return found_text, {"mode": "local", "resolved_path": local_path, "bucket": ""}

    return "", {"mode": "missing", "resolved_path": "", "bucket": ""}


def _resolve_file_presence(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    relative_or_abs: str,
) -> Dict[str, Any]:
    target = _norm_path(relative_or_abs)
    if not target:
        return {"path": "", "exists": False, "source": "missing", "resolved_path": "", "bucket": ""}

    if source_workflow_id:
        supabase = _get_supabase(state)
        for bucket in ARTIFACT_BUCKET_CANDIDATES:
            for full_path in _storage_try_candidates(prefixes, target):
                try:
                    supabase.storage.from_(bucket).download(full_path)
                    return {
                        "path": target,
                        "exists": True,
                        "source": "supabase",
                        "resolved_path": full_path,
                        "bucket": bucket,
                    }
                except Exception:
                    pass

    if workflow_dir:
        local_path = _join_workflow_path(workflow_dir, target)
        if os.path.exists(local_path):
            return {
                "path": target,
                "exists": True,
                "source": "local",
                "resolved_path": local_path,
                "bucket": "",
            }

    return {"path": target, "exists": False, "source": "missing", "resolved_path": "", "bucket": ""}


def _restore_files_locally(
    state: Dict[str, Any],
    workflow_dir: str,
    file_checks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    restored_root = ""
    restored_files: List[str] = []
    failed_files: List[str] = []

    if not workflow_dir:
        return {"restored_root": "", "restored_files": [], "failed_files": []}

    restored_root = os.path.join(workflow_dir, "restored_system_rtl")
    os.makedirs(restored_root, exist_ok=True)

    supabase = _get_supabase(state)

    for item in file_checks:
        rel_path = _norm_path(item.get("path"))
        bucket = _norm_path(item.get("bucket"))
        resolved_path = _norm_path(item.get("resolved_path"))
        if not rel_path:
            continue

        local_path = os.path.join(restored_root, rel_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        if os.path.isfile(local_path):
            restored_files.append(rel_path)
            continue

        blob = b""
        if bucket and resolved_path:
            blob = _storage_download_bytes(supabase, bucket, resolved_path)

        if not blob and resolved_path and os.path.isfile(resolved_path):
            try:
                with open(resolved_path, "rb") as f:
                    blob = f.read()
            except Exception:
                blob = b""

        if not blob:
            failed_files.append(rel_path)
            continue

        try:
            with open(local_path, "wb") as f:
                f.write(blob)
            restored_files.append(rel_path)
        except Exception:
            failed_files.append(rel_path)

    return {"restored_root": restored_root, "restored_files": restored_files, "failed_files": failed_files}


def _parse_filelist(text: str) -> List[str]:
    out: List[str] = []
    for line in (text or "").splitlines():
        item = line.strip()
        if not item or item == "(empty)":
            continue
        out.append(item)
    return out


def _build_required_file_checks(
    state: Dict[str, Any],
    workflow_dir: str,
    source_workflow_id: str,
    prefixes: List[str],
    required_paths: List[str],
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    seen = set()
    for rel in required_paths:
        norm = _norm_path(rel)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        meta = _resolve_file_presence(
            state=state,
            workflow_dir=workflow_dir,
            source_workflow_id=source_workflow_id,
            prefixes=prefixes,
            relative_or_abs=norm,
        )
        checks.append({"path": norm, "required": True, **meta})
    return checks


def _compile_status(compile_summary: Dict[str, Any]) -> Dict[str, Any]:
    sim = compile_summary.get("sim") or {}
    phys = compile_summary.get("phys") or {}

    sim_pass = bool(
        sim.get("iverilog_ok_pass2", sim.get("iverilog_ok_pass1", False))
        and sim.get("verilator_ok_pass2", sim.get("verilator_ok_pass1", False))
    )

    phys_skipped = bool(phys.get("skipped"))
    phys_pass = True if phys_skipped else bool(
        phys.get("iverilog_ok_pass2", phys.get("iverilog_ok_pass1", False))
        and phys.get("verilator_ok_pass2", phys.get("verilator_ok_pass1", False))
    )

    return {
        "sim_status": "pass" if sim_pass else "fail",
        "phys_status": "skipped" if phys_skipped else ("pass" if phys_pass else "fail"),
        "ready_for_cosim": sim_pass,
        "ready_for_phys_handoff": sim_pass and (phys_pass or phys_skipped),
    }


def _build_package(
    source_workflow_id: str,
    resolution_meta: Dict[str, Any],
    integration_intent: Dict[str, Any],
    integration_meta: Dict[str, Any],
    compile_summary: Dict[str, Any],
    compile_meta: Dict[str, Any],
    soc_top_sim_text: str,
    soc_top_sim_meta: Dict[str, Any],
    soc_top_phys_text: str,
    soc_top_phys_meta: Dict[str, Any],
    sim_filelist: List[str],
    sim_filelist_meta: Dict[str, Any],
    phys_filelist: List[str],
    phys_filelist_meta: Dict[str, Any],
    phys_lib_filelist: List[str],
    phys_lib_filelist_meta: Dict[str, Any],
    discovered_rtl: List[str],
    discovered_rtl_meta: Dict[str, Any],
    discovered_libs: List[str],
    discovered_libs_meta: Dict[str, Any],
    pass1_sim_present: bool,
    pass1_sim_meta: Dict[str, Any],
    pass1_phys_present: bool,
    pass1_phys_meta: Dict[str, Any],
    required_file_checks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    status = _compile_status(compile_summary)
    top = integration_intent.get("top") or {}

    missing_required_files = [x["path"] for x in required_file_checks if not x.get("exists")]

    return {
        "package_type": "system_rtl",
        "package_version": "1.0",
        "generated_at": _now(),
        "source_workflow_id": source_workflow_id,
        "source_resolution": {
            "mode": resolution_meta.get("mode") or "unknown",
            "artifact_bucket": resolution_meta.get("artifact_bucket") or "",
            "storage_prefixes": resolution_meta.get("storage_prefixes") or [],
            "workflow_row_found": bool(resolution_meta.get("workflow_row_found")),
            "workflow_name": resolution_meta.get("workflow_name") or "",
        },
        "top": {
            "base_name": top.get("base_name") or "soc_top",
            "sim_module": top.get("sim_module") or "soc_top_sim",
            "phys_module": top.get("phys_module") or "soc_top_phys",
        },
        "artifacts": {
            "integration_intent": {
                "exists": bool(integration_intent),
                "source": integration_meta.get("mode"),
                "resolved_path": integration_meta.get("resolved_path"),
                "bucket": integration_meta.get("bucket"),
            },
            "soc_top_sim": {
                "exists": bool(soc_top_sim_text),
                "source": soc_top_sim_meta.get("mode"),
                "resolved_path": soc_top_sim_meta.get("resolved_path"),
                "bucket": soc_top_sim_meta.get("bucket"),
            },
            "soc_top_phys": {
                "exists": bool(soc_top_phys_text),
                "source": soc_top_phys_meta.get("mode"),
                "resolved_path": soc_top_phys_meta.get("resolved_path"),
                "bucket": soc_top_phys_meta.get("bucket"),
            },
            "system_rtl_filelist_sim": {
                "exists": bool(sim_filelist),
                "source": sim_filelist_meta.get("mode"),
                "resolved_path": sim_filelist_meta.get("resolved_path"),
                "bucket": sim_filelist_meta.get("bucket"),
            },
            "system_rtl_filelist_phys": {
                "exists": bool(phys_filelist),
                "source": phys_filelist_meta.get("mode"),
                "resolved_path": phys_filelist_meta.get("resolved_path"),
                "bucket": phys_filelist_meta.get("bucket"),
            },
            "system_lib_filelist_phys": {
                "exists": bool(phys_lib_filelist),
                "source": phys_lib_filelist_meta.get("mode"),
                "resolved_path": phys_lib_filelist_meta.get("resolved_path"),
                "bucket": phys_lib_filelist_meta.get("bucket"),
            },
            "system_full_compile_summary": {
                "exists": bool(compile_summary),
                "source": compile_meta.get("mode"),
                "resolved_path": compile_meta.get("resolved_path"),
                "bucket": compile_meta.get("bucket"),
            },
            "soc_top_sim_pass1": {
                "exists": pass1_sim_present,
                "source": pass1_sim_meta.get("mode"),
                "resolved_path": pass1_sim_meta.get("resolved_path"),
                "bucket": pass1_sim_meta.get("bucket"),
            },
            "soc_top_phys_pass1": {
                "exists": pass1_phys_present,
                "source": pass1_phys_meta.get("mode"),
                "resolved_path": pass1_phys_meta.get("resolved_path"),
                "bucket": pass1_phys_meta.get("bucket"),
            },
        },
        "filelists": {
            "sim": sim_filelist,
            "phys": phys_filelist,
            "phys_libs": phys_lib_filelist,
            "discovered_rtl": discovered_rtl,
            "discovered_libs": discovered_libs,
        },
        "compile_status": {
            **status,
            "raw_summary": compile_summary,
        },
        "validation_readiness": {
            "missing_required_files": missing_required_files,
            "overall_status": "ready" if status["ready_for_cosim"] and not missing_required_files else "not_ready",
        },
    }


def _markdown_summary(pkg: Dict[str, Any]) -> str:
    lines = [
        "# System RTL Package Summary",
        "",
        f"- Generated at: {pkg.get('generated_at')}",
        f"- Source workflow id: `{pkg.get('source_workflow_id') or 'unavailable'}`",
        f"- Sim top: `{((pkg.get('top') or {}).get('sim_module') or '')}`",
        f"- Phys top: `{((pkg.get('top') or {}).get('phys_module') or '')}`",
        f"- Sim status: `{((pkg.get('compile_status') or {}).get('sim_status') or '')}`",
        f"- Phys status: `{((pkg.get('compile_status') or {}).get('phys_status') or '')}`",
        f"- Ready for cosim: `{((pkg.get('compile_status') or {}).get('ready_for_cosim') or False)}`",
        "",
        "## Filelists",
        "",
        f"- sim rtl count: `{len(((pkg.get('filelists') or {}).get('sim') or []))}`",
        f"- phys rtl count: `{len(((pkg.get('filelists') or {}).get('phys') or []))}`",
        f"- phys lib count: `{len(((pkg.get('filelists') or {}).get('phys_libs') or []))}`",
        "",
        "## Missing required files",
        "",
    ]
    missing = ((pkg.get("validation_readiness") or {}).get("missing_required_files")) or []
    if not missing:
        lines.append("- none")
    else:
        for item in missing:
            lines.append(f"- `{item}`")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = os.path.abspath(state.get("workflow_dir")) if _is_nonempty_str(state.get("workflow_dir")) else ""

    print(f"\n📦 Running {AGENT_NAME}")

    source_workflow_id = _get_source_workflow_id(state)
    if not source_workflow_id:
        state["status"] = "❌ system rtl workflow id missing"
        return state

    resolution_meta: Dict[str, Any] = {
        "mode": "unknown",
        "artifact_bucket": "",
        "storage_prefixes": [],
        "workflow_row_found": False,
        "workflow_name": "",
    }

    prefixes: List[str] = []
    try:
        supabase = _get_supabase(state)
        wf_row = _workflow_row(supabase, source_workflow_id)
        prefixes = _workflow_storage_prefixes(state, source_workflow_id, wf_row)
        resolution_meta = {
            "mode": "supabase_or_local",
            "artifact_bucket": "",
            "storage_prefixes": prefixes,
            "workflow_row_found": bool(wf_row),
            "workflow_name": (wf_row or {}).get("name") or "",
        }
    except Exception:
        prefixes = []

    integration_intent, integration_meta = _resolve_json_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_integration_intent",
        path_keys=["system_integration_intent_path"],
        suffix_candidates=SYSTEM_INTEGRATION_INTENT_SUFFIXES,
    )
    compile_summary, compile_meta = _resolve_json_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_full_compile_summary",
        path_keys=["system_full_compile_summary_path"],
        suffix_candidates=FULL_COMPILE_SUMMARY_SUFFIXES,
    )

    soc_top_sim_text, soc_top_sim_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="soc_top_sim_code",
        path_keys=["soc_top_sim_path", "system_top_sim_path"],
        suffix_candidates=SOC_TOP_SIM_SUFFIXES,
    )
    soc_top_phys_text, soc_top_phys_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="soc_top_phys_code",
        path_keys=["soc_top_phys_path", "system_top_phys_path"],
        suffix_candidates=SOC_TOP_PHYS_SUFFIXES,
    )

    sim_filelist_text, sim_filelist_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_rtl_filelist_sim_text",
        path_keys=["system_rtl_filelist_sim_path"],
        suffix_candidates=RTL_FILELIST_SIM_SUFFIXES,
    )
    phys_filelist_text, phys_filelist_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_rtl_filelist_phys_text",
        path_keys=["system_rtl_filelist_phys_path"],
        suffix_candidates=RTL_FILELIST_PHYS_SUFFIXES,
    )
    phys_lib_filelist_text, phys_lib_filelist_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_lib_filelist_phys_text",
        path_keys=["system_lib_filelist_phys_path"],
        suffix_candidates=LIB_FILELIST_PHYS_SUFFIXES,
    )
    discovered_rtl_text, discovered_rtl_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_discovered_rtl_text",
        path_keys=["system_discovered_rtl_path"],
        suffix_candidates=DISCOVERED_RTL_SUFFIXES,
    )
    discovered_libs_text, discovered_libs_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="system_discovered_libs_text",
        path_keys=["system_discovered_libs_path"],
        suffix_candidates=DISCOVERED_LIBS_SUFFIXES,
    )

    pass1_sim_text, pass1_sim_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="soc_top_sim_pass1_code",
        path_keys=["soc_top_sim_pass1_path"],
        suffix_candidates=PASS1_SIM_SUFFIXES,
    )
    pass1_phys_text, pass1_phys_meta = _resolve_text_asset(
        state, workflow_dir, source_workflow_id, prefixes,
        explicit_state_key="soc_top_phys_pass1_code",
        path_keys=["soc_top_phys_pass1_path"],
        suffix_candidates=PASS1_PHYS_SUFFIXES,
    )

    sim_filelist = _parse_filelist(sim_filelist_text)
    phys_filelist = _parse_filelist(phys_filelist_text)
    phys_lib_filelist = _parse_filelist(phys_lib_filelist_text)
    discovered_rtl = _parse_filelist(discovered_rtl_text)
    discovered_libs = _parse_filelist(discovered_libs_text)

    required_paths = [
        "system/integration/system_integration_intent.json",
        "system/integration/soc_top_sim.sv",
        "system/integration/soc_top_phys.sv",
        "system/integration/system_rtl_filelist_sim.txt",
        "system/integration/system_rtl_filelist_phys.txt",
        "system/integration/system_full_compile_summary.json",
    ]
    required_paths.extend(sim_filelist)
    required_paths.extend(phys_filelist)
    required_paths.extend(phys_lib_filelist)

    required_file_checks = _build_required_file_checks(
        state=state,
        workflow_dir=workflow_dir,
        source_workflow_id=source_workflow_id,
        prefixes=prefixes,
        required_paths=required_paths,
    )

    restore_info = _restore_files_locally(
        state=state,
        workflow_dir=workflow_dir,
        file_checks=required_file_checks,
    )

    pkg = _build_package(
        source_workflow_id=source_workflow_id,
        resolution_meta=resolution_meta,
        integration_intent=integration_intent,
        integration_meta=integration_meta,
        compile_summary=compile_summary,
        compile_meta=compile_meta,
        soc_top_sim_text=soc_top_sim_text,
        soc_top_sim_meta=soc_top_sim_meta,
        soc_top_phys_text=soc_top_phys_text,
        soc_top_phys_meta=soc_top_phys_meta,
        sim_filelist=sim_filelist,
        sim_filelist_meta=sim_filelist_meta,
        phys_filelist=phys_filelist,
        phys_filelist_meta=phys_filelist_meta,
        phys_lib_filelist=phys_lib_filelist,
        phys_lib_filelist_meta=phys_lib_filelist_meta,
        discovered_rtl=discovered_rtl,
        discovered_rtl_meta=discovered_rtl_meta,
        discovered_libs=discovered_libs,
        discovered_libs_meta=discovered_libs_meta,
        pass1_sim_present=bool(pass1_sim_text),
        pass1_sim_meta=pass1_sim_meta,
        pass1_phys_present=bool(pass1_phys_text),
        pass1_phys_meta=pass1_phys_meta,
        required_file_checks=required_file_checks,
    )

    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workflow_dir": workflow_dir,
        "source_workflow_id": source_workflow_id,
        "resolution_meta": resolution_meta,
        "required_file_check_count": len(required_file_checks),
        "restore_info": restore_info,
    }

    _record_text(workflow_id, PACKAGE_JSON, json.dumps(pkg, indent=2))
    _record_text(workflow_id, SUMMARY_MD, _markdown_summary(pkg))
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["source_rtl_workflow_id"] = source_workflow_id
    state["system_rtl_package"] = pkg
    state["system_rtl_package_path"] = f"{OUTPUT_SUBDIR}/{PACKAGE_JSON}"
    state["system_rtl_package_summary_path"] = f"{OUTPUT_SUBDIR}/{SUMMARY_MD}"

    state["system_rtl_restored_root"] = restore_info.get("restored_root") or ""
    state["system_rtl_restored_files"] = restore_info.get("restored_files") or []
    state["system_rtl_restore_failed_files"] = restore_info.get("failed_files") or []

    system_block = state.setdefault("system", {})
    if isinstance(system_block, dict):
        system_block["rtl_package"] = pkg
        system_block["rtl_package_path"] = state["system_rtl_package_path"]
        system_block["source_rtl_workflow_id"] = source_workflow_id

    overall_status = ((pkg.get("validation_readiness") or {}).get("overall_status")) or "not_ready"
    state["status"] = (
        "✅ System RTL package complete"
        if overall_status == "ready"
        else "⚠️ System RTL package completed with missing or non-ready artifacts"
    )
    return state
