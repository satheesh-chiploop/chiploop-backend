import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Liberty Characterization Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds", "generate_gds"}


def _module_name(state: dict) -> str:
    return str(state.get("analog_macro_module") or "analog_macro").strip()


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "lib_char")
    os.makedirs(stage_dir, exist_ok=True)

    module_name = _module_name(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    prior_lib = state.get("analog_macro_lib")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module_name,
        "spice": spice_path,
        "prior_lib": prior_lib,
    }

    if not _enabled(state):
        summary.update({"status": "skipped", "reason": "analog_physical_mode_not_generate_gds"})
    elif not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "deferred", "reason": "analog_spice_missing"})
    else:
        ngspice_bin = shutil.which("ngspice")
        deck_path = os.path.join(stage_dir, "characterize_ngspice.sp")
        deck = "\n".join([
            f".include {os.path.abspath(spice_path)}",
            "* Placeholder characterization deck. Real Liberty requires block-specific stimuli and measurements.",
            ".control",
            "op",
            "write op.raw",
            "quit",
            ".endc",
            ".end",
            "",
        ])
        with open(deck_path, "w", encoding="utf-8") as fh:
            fh.write(deck)

        if not ngspice_bin:
            summary.update({
                "status": "tool_unavailable",
                "reason": "ngspice_not_installed",
                "characterization_deck": deck_path,
                "note": "Liberty was not regenerated. Prior abstract LIB remains provisional.",
            })
        else:
            cp = run_command(state, "analog_lib_char_ngspice", [ngspice_bin, "-b", deck_path], cwd=stage_dir, timeout_sec=1800)
            log = (cp.stdout or "") + (cp.stderr or "")
            log_path = os.path.join(stage_dir, "ngspice_characterization.log")
            with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(log)
            summary.update({
                "status": "measured_pending_liberty_model",
                "reason": "block_specific_liberty_template_required",
                "return_code": cp.returncode,
                "log": log_path,
                "characterization_deck": deck_path,
                "note": "ngspice ran, but no replacement Liberty is emitted without a block-specific timing/power arc template.",
            })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "liberty_characterization_summary.json", json.dumps(summary, indent=2))
    if os.path.exists(os.path.join(stage_dir, "characterize_ngspice.sp")):
        with open(os.path.join(stage_dir, "characterize_ngspice.sp"), "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "characterize_ngspice.sp", fh.read())

    state["analog_liberty_characterization"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
