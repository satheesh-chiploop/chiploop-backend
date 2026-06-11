import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog LEF Extraction Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    return str(state.get("analog_macro_module") or "analog_macro").strip()


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "lef_extract")
    os.makedirs(stage_dir, exist_ok=True)

    module_name = _module_name(state)
    gds_path = state.get("analog_macro_gds")
    prior_lef = state.get("analog_macro_lef")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module_name,
        "gds": gds_path,
        "prior_lef": prior_lef,
    }

    if not _enabled(state):
        summary.update({"status": "skipped", "reason": "analog_physical_mode_not_generate_gds"})
    elif not isinstance(gds_path, str) or not os.path.exists(gds_path):
        summary.update({"status": "deferred", "reason": "analog_macro_gds_missing"})
    else:
        magic_bin = shutil.which("magic")
        tcl_path = os.path.join(stage_dir, "extract_lef.tcl")
        lef_path = os.path.join(stage_dir, f"{module_name}.lef")
        tcl = "\n".join([
            "drc off",
            f"gds read {os.path.abspath(gds_path)}",
            f"load {module_name}",
            "select top cell",
            f"lef write {os.path.abspath(lef_path)}",
            "quit -noprompt",
            "",
        ])
        with open(tcl_path, "w", encoding="utf-8") as fh:
            fh.write(tcl)

        if magic_bin:
            cp = run_command(state, "analog_lef_extract_magic", [magic_bin, "-dnull", "-noconsole", tcl_path], cwd=stage_dir, timeout_sec=1800)
            log = (cp.stdout or "") + (cp.stderr or "")
            log_path = os.path.join(stage_dir, "magic_lef_extract.log")
            with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(log)
            if os.path.exists(lef_path) and os.path.getsize(lef_path) > 0:
                state["analog_macro_lef"] = lef_path
                digital = state.setdefault("digital", {})
                if isinstance(digital, dict):
                    digital["macro_lefs"] = list(dict.fromkeys((digital.get("macro_lefs") or []) + [lef_path]))
                summary.update({"status": "extracted", "lef": lef_path, "log": log_path, "return_code": cp.returncode})
            else:
                summary.update({"status": "failed", "reason": "lef_not_produced", "log": log_path, "return_code": cp.returncode})
        else:
            summary.update({
                "status": "tool_unavailable",
                "reason": "magic_not_installed",
                "extract_script": tcl_path,
                "note": "LEF was not regenerated from GDS. Prior abstract LEF remains provisional.",
            })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", "lef_extract_summary.json", json.dumps(summary, indent=2))
    if os.path.exists(os.path.join(stage_dir, "extract_lef.tcl")):
        with open(os.path.join(stage_dir, "extract_lef.tcl"), "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", "extract_lef.tcl", fh.read())
    if summary.get("lef") and os.path.exists(str(summary["lef"])):
        with open(str(summary["lef"]), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lef_extract", f"{module_name}.lef", fh.read())

    state["analog_lef_extraction"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
