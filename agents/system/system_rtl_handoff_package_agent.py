# System RTL Handoff Package Agent (FINAL - Production Ready)

import datetime
import json
import os
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System RTL Handoff Package Agent"
OUTPUT_SUBDIR = "system/package"

PACKAGE_JSON = "system_rtl_package.json"
SUMMARY_MD = "system_rtl_package_summary.md"
DEBUG_JSON = "system_rtl_package_debug.json"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id, filename, content):
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


def _safe_read(path):
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _safe_json(path):
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _parse_filelist(text: str):
    return [x.strip() for x in text.splitlines() if x.strip() and x.strip() != "(empty)"]


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"
    workflow_dir = state.get("workflow_dir") or ""

    print(f"\n📦 Running {AGENT_NAME}")

    integration_dir = os.path.join(workflow_dir, "system/integration")

    # ---------- Resolve LOCAL artifacts ----------
    integration_intent = _safe_json(os.path.join(integration_dir, "system_integration_intent.json"))
    compile_summary = _safe_json(os.path.join(integration_dir, "system_full_compile_summary.json"))

    soc_top_sim = _safe_read(os.path.join(integration_dir, "soc_top_sim.sv"))
    soc_top_phys = _safe_read(os.path.join(integration_dir, "soc_top_phys.sv"))

    sim_filelist = _parse_filelist(_safe_read(os.path.join(integration_dir, "system_rtl_filelist_sim.txt")))
    phys_filelist = _parse_filelist(_safe_read(os.path.join(integration_dir, "system_rtl_filelist_phys.txt")))
    lib_filelist = _parse_filelist(_safe_read(os.path.join(integration_dir, "system_lib_filelist_phys.txt")))

    # ---------- Compile logs ----------
    compile_logs = {
        "iverilog_sim": bool(_safe_read(os.path.join(integration_dir, "system_full_compile_iverilog_sim_pass1.log"))),
        "verilator_sim": bool(_safe_read(os.path.join(integration_dir, "system_full_compile_verilator_sim_pass1.log"))),
        "iverilog_phys": bool(_safe_read(os.path.join(integration_dir, "system_full_compile_iverilog_phys_pass1.log"))),
        "verilator_phys": bool(_safe_read(os.path.join(integration_dir, "system_full_compile_verilator_phys_pass1.log"))),
    }

    # ---------- Compile status ----------
    sim = compile_summary.get("sim", {}) if compile_summary else {}
    phys = compile_summary.get("phys", {}) if compile_summary else {}

    # ---------- SIM COMPILE DETECTION ----------
    sim_ok_summary = (
        sim.get("iverilog_ok_pass1") and sim.get("verilator_ok_pass1")
    )

    sim_ok_logs = (
        compile_logs.get("iverilog_sim") and compile_logs.get("verilator_sim")
    )

    sim_ok = sim_ok_summary if sim_ok_summary is not None else sim_ok_logs

    # ---------- FILELIST VALIDATION ----------
    sim_filelist_ok = len(sim_filelist) > 0

    # ---------- FINAL SIM STATUS ----------
    sim_ok = bool(sim_ok and sim_filelist_ok)

    # ---------- PHYS ----------
    phys_skipped = phys.get("skipped", True)


    if not sim_filelist:
        print("⚠️ WARNING: Sim filelist is empty")

    if not phys_filelist:
        print("⚠️ WARNING: Phys filelist is empty")

    # ---------- Build package ----------
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

    

    # ---------- Save ----------
    _record(workflow_id, PACKAGE_JSON, json.dumps(pkg, indent=2))
    _record(workflow_id, SUMMARY_MD, f"# RTL Package\n\nReady for cosim: {pkg['ready_for_cosim']}")
    _record(workflow_id, DEBUG_JSON, json.dumps(pkg, indent=2))

    state["system_rtl_package"] = pkg
    state["status"] = "✅ RTL Package Ready" if pkg["ready_for_cosim"] else "⚠️ RTL Package Not Ready"

    return state