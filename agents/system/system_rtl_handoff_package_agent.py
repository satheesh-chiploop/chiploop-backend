# System RTL Handoff Package Agent (Supabase-first, production-worthy)

import datetime
import json
import os
from typing import Any, Dict, List, Tuple

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System RTL Handoff Package Agent"
OUTPUT_SUBDIR = "system/package"
ARTIFACT_BUCKET = "artifacts"

PACKAGE_JSON = "system_rtl_package.json"
SUMMARY_MD = "system_rtl_package_summary.md"
DEBUG_JSON = "system_rtl_package_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, content: str):
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


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _get_supabase(state: Dict[str, Any]):
    client = state.get("supabase_client")
    if client is None:
        raise RuntimeError("supabase_client missing from state")
    return client


def _workflow_row(supabase, workflow_id: str) -> Dict[str, Any]:
    if not workflow_id:
        return {}
    try:
        resp = (
            supabase.table("workflows")
            .select("id,user_id,name,loop_type,artifacts")
            .eq("id", workflow_id)
            .single()
            .execute()
        )
        return resp.data or {}
    except Exception:
        return {}


def _workflow_storage_prefixes(state: Dict[str, Any], source_workflow_id: str, workflow_row: Dict[str, Any]) -> List[str]:
    prefixes: List[str] = []

    for key in ("artifact_prefix", "source_artifact_prefix"):
        v = state.get(key)
        if isinstance(v, str) and v.strip():
            prefixes.append(_norm_path(v).rstrip("/") + "/")

    prefixes.append(f"backend/workflows/{source_workflow_id}/")

    user_id = (workflow_row or {}).get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        prefixes.append(f"artifacts/{user_id}/{source_workflow_id}/")
        prefixes.append(f"{user_id}/{source_workflow_id}/")

    seen = set()
    out: List[str] = []
    for p in prefixes:
        p = _norm_path(p)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_supabase_artifact_path(supabase, prefixes: List[str], rel_or_abs: str) -> str:
    target = _norm_path(rel_or_abs)
    if not target:
        return ""

    candidates: List[str] = []
    if target.startswith("backend/workflows/") or target.startswith("artifacts/"):
        candidates.append(target)
    else:
        for prefix in prefixes:
            candidates.append(f"{prefix.rstrip('/')}/{target}")
        candidates.append(target)

    seen = set()
    ordered: List[str] = []
    for c in candidates:
        c = _norm_path(c)
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    for candidate in ordered:
        try:
            supabase.storage.from_(ARTIFACT_BUCKET).download(candidate)
            return candidate
        except Exception:
            pass
    return ""


def _load_text_from_supabase(supabase, prefixes: List[str], rel_or_abs: str) -> Tuple[str, str]:
    resolved = _resolve_supabase_artifact_path(supabase, prefixes, rel_or_abs)
    if not resolved:
        return "", ""
    try:
        raw = supabase.storage.from_(ARTIFACT_BUCKET).download(resolved)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return text, resolved
    except Exception:
        return "", ""


def _load_json_from_supabase(supabase, prefixes: List[str], rel_or_abs: str) -> Tuple[Dict[str, Any], str]:
    text, resolved = _load_text_from_supabase(supabase, prefixes, rel_or_abs)
    if not text:
        return {}, ""
    try:
        obj = json.loads(text)
        return (obj if isinstance(obj, dict) else {}), resolved
    except Exception:
        return {}, ""


def _parse_filelist(text: str) -> List[str]:
    return [x.strip() for x in text.splitlines() if x.strip() and x.strip() != "(empty)"]


def run_agent(state: dict) -> dict:
    workflow_id = str(state.get("workflow_id") or "default")

    print(f"\n📦 Running {AGENT_NAME}")

    debug: Dict[str, Any] = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "workflow_id": workflow_id,
        "resolution_mode": "supabase_first",
        "artifacts": {},
        "warnings": [],
    }

    try:
        supabase = _get_supabase(state)
        wf_row = _workflow_row(supabase, workflow_id)
        prefixes = _workflow_storage_prefixes(state, workflow_id, wf_row)

        debug["workflow_row_found"] = bool(wf_row)
        debug["storage_prefixes"] = prefixes

        integration_intent, integration_intent_path = _load_json_from_supabase(
            supabase, prefixes, "system/integration/system_integration_intent.json"
        )
        compile_summary, compile_summary_path = _load_json_from_supabase(
            supabase, prefixes, "system/integration/system_full_compile_summary.json"
        )
        soc_top_sim, soc_top_sim_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/soc_top_sim.sv"
        )
        soc_top_phys, soc_top_phys_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/soc_top_phys.sv"
        )
        sim_filelist_text, sim_filelist_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_rtl_filelist_sim.txt"
        )
        phys_filelist_text, phys_filelist_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_rtl_filelist_phys.txt"
        )
        lib_filelist_text, lib_filelist_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_lib_filelist_phys.txt"
        )

        iverilog_sim_log, iverilog_sim_log_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_full_compile_iverilog_sim_pass1.log"
        )
        verilator_sim_log, verilator_sim_log_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_full_compile_verilator_sim_pass1.log"
        )
        iverilog_phys_log, iverilog_phys_log_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_full_compile_iverilog_phys_pass1.log"
        )
        verilator_phys_log, verilator_phys_log_path = _load_text_from_supabase(
            supabase, prefixes, "system/integration/system_full_compile_verilator_phys_pass1.log"
        )

        debug["artifacts"] = {
            "system_integration_intent_json": integration_intent_path,
            "system_full_compile_summary_json": compile_summary_path,
            "soc_top_sim_sv": soc_top_sim_path,
            "soc_top_phys_sv": soc_top_phys_path,
            "system_rtl_filelist_sim_txt": sim_filelist_path,
            "system_rtl_filelist_phys_txt": phys_filelist_path,
            "system_lib_filelist_phys_txt": lib_filelist_path,
            "iverilog_sim_log": iverilog_sim_log_path,
            "verilator_sim_log": verilator_sim_log_path,
            "iverilog_phys_log": iverilog_phys_log_path,
            "verilator_phys_log": verilator_phys_log_path,
        }

        sim_filelist = _parse_filelist(sim_filelist_text)
        phys_filelist = _parse_filelist(phys_filelist_text)
        lib_filelist = _parse_filelist(lib_filelist_text)

        compile_logs = {
            "iverilog_sim": bool(iverilog_sim_log),
            "verilator_sim": bool(verilator_sim_log),
            "iverilog_phys": bool(iverilog_phys_log),
            "verilator_phys": bool(verilator_phys_log),
        }

        sim = compile_summary.get("sim", {}) if compile_summary else {}
        phys = compile_summary.get("phys", {}) if compile_summary else {}

        iverilog_summary = sim.get("iverilog_ok_pass1")
        verilator_summary = sim.get("verilator_ok_pass1")
        summary_has_both = (iverilog_summary is not None and verilator_summary is not None)
        sim_ok_summary = bool(iverilog_summary and verilator_summary)

        sim_ok_logs = bool(
            compile_logs.get("iverilog_sim") and compile_logs.get("verilator_sim")
        )

        sim_ok = sim_ok_summary if summary_has_both else sim_ok_logs
        sim_filelist_ok = len(sim_filelist) > 0
        sim_ok = bool(sim_ok and sim_filelist_ok)

        phys_skipped = phys.get("skipped", True)

        if not compile_summary:
            debug["warnings"].append("compile_summary_missing")
        if not sim_filelist:
            debug["warnings"].append("sim_filelist_missing")
        if not phys_filelist:
            debug["warnings"].append("phys_filelist_missing")
        if not lib_filelist:
            debug["warnings"].append("lib_filelist_missing")

        pkg = {
            "package_type": "system_rtl",
            "generated_at": _now(),
            "top": {
                "sim": "soc_top_sim",
                "phys": "soc_top_phys"
            },
            "filelists": {
                "sim": sim_filelist,
                "phys": phys_filelist,
                "libs": lib_filelist
            },
            "compile": {
                "sim": "pass" if sim_ok else "fail",
                "phys": "skipped" if phys_skipped else "pass",
                "logs_present": compile_logs
            },
            "artifacts": {
                "soc_top_sim": bool(soc_top_sim),
                "soc_top_phys": bool(soc_top_phys),
                "integration_intent": bool(integration_intent)
            },
            "ready_for_cosim": bool(sim_ok and sim_filelist_ok)
        }

        _record(workflow_id, PACKAGE_JSON, json.dumps(pkg, indent=2))
        _record(workflow_id, SUMMARY_MD, f"# RTL Package\n\nReady for cosim: {pkg['ready_for_cosim']}")
        _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))

        state["system_rtl_package"] = pkg
        state["status"] = "✅ RTL Package Ready" if pkg["ready_for_cosim"] else "⚠️ RTL Package Not Ready"
        return state

    except Exception as exc:
        debug["error"] = str(exc)
        _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))
        state["status"] = f"❌ RTL Package failed: {exc}"
        return state
