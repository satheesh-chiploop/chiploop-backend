import json
import os
import re
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
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _pdk_root_host(state: dict) -> str:
    return os.path.abspath(str(
        state.get("pdk_root_host")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or "/root/chiploop-backend/backend/pdk"
    ))


def _prepare_ngspice_spice(src: str, dst: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    lines = []
    has_sky130_lib = bool(re.search(r"^\s*\.lib\s+.*sky130\.lib\.spice\"?\s+tt\b", text, flags=re.IGNORECASE | re.MULTILINE))
    for line in text.splitlines():
        if has_sky130_lib and re.search(r"^\s*\.include\s+.*sky130\.lib\.spice\"?", line, flags=re.IGNORECASE):
            continue
        lines.append(line)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "lib_char")
    os.makedirs(stage_dir, exist_ok=True)

    module_name = _module_name(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    prior_lib = state.get("analog_macro_lib")
    pdk_root_host = _pdk_root_host(state)
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": module_name,
        "spice": spice_path,
        "prior_lib": prior_lib,
        "pdk_root_host": pdk_root_host,
    }

    if not _enabled(state):
        summary.update({"status": "skipped", "reason": "analog_physical_mode_not_generate_gds"})
    elif not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "failed", "reason": "analog_spice_missing"})
    else:
        ngspice_bin = shutil.which("ngspice")
        deck_path = os.path.join(stage_dir, "characterize_ngspice.sp")
        staged_spice = os.path.join(stage_dir, os.path.basename(spice_path) or f"{module_name}.spice")
        if os.path.abspath(spice_path) != os.path.abspath(staged_spice):
            _prepare_ngspice_spice(spice_path, staged_spice)
        else:
            _prepare_ngspice_spice(spice_path, staged_spice)
        deck = "\n".join([
            f'.include "{staged_spice}"',
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
                "status": "failed",
                "reason": "ngspice_not_installed",
                "characterization_deck": deck_path,
                "note": "Liberty characterization did not run because ngspice is not installed or not on PATH.",
            })
        else:
            cp = run_command(
                state,
                "analog_lib_char_ngspice",
                [ngspice_bin, "-b", deck_path],
                cwd=stage_dir,
                timeout_sec=1800,
                env={"PDK_ROOT": pdk_root_host},
            )
            log = (cp.stdout or "") + (cp.stderr or "")
            log_path = os.path.join(stage_dir, "ngspice_characterization.log")
            with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(log)
            if cp.returncode == 0:
                if isinstance(prior_lib, str) and os.path.exists(prior_lib):
                    state["analog_macro_lib"] = prior_lib
                    digital = state.setdefault("digital", {})
                    if isinstance(digital, dict):
                        digital["macro_libs"] = list(dict.fromkeys((digital.get("macro_libs") or []) + [prior_lib]))
                summary.update({
                    "status": "validated",
                    "reason": "liberty_not_produced",
                    "return_code": cp.returncode,
                    "log": log_path,
                    "characterization_deck": deck_path,
                    "generated_liberty": False,
                    "lib": prior_lib if isinstance(prior_lib, str) and os.path.exists(prior_lib) else "",
                    "note": "ngspice ran successfully. No replacement Liberty was produced, so the flow records SPICE validation and preserves any existing macro Liberty as an input artifact rather than marking it generated.",
                })
            else:
                summary.update({
                    "status": "failed",
                    "reason": "ngspice_failed",
                    "return_code": cp.returncode,
                    "log": log_path,
                    "characterization_deck": deck_path,
                    "log_tail": log[-2000:],
                })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "liberty_characterization_summary.json", json.dumps(summary, indent=2))
    if os.path.exists(os.path.join(stage_dir, "characterize_ngspice.sp")):
        with open(os.path.join(stage_dir, "characterize_ngspice.sp"), "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "characterize_ngspice.sp", fh.read())
    if os.path.exists(os.path.join(stage_dir, "ngspice_characterization.log")):
        with open(os.path.join(stage_dir, "ngspice_characterization.log"), "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/lib_char", "ngspice_characterization.log", fh.read())

    state["analog_liberty_characterization"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if _enabled(state) and summary["status"] not in {"generated", "validated"}:
        detail = summary.get("log_tail") or ""
        raise RuntimeError(
            f"Analog Liberty characterization failed: {summary.get('reason') or summary['status']}"
            + (f"\nngspice log tail:\n{detail}" if detail else "")
        )
    return state
