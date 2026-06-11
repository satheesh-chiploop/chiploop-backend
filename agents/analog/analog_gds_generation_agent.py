import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict

from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog GDS Generation Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    return str(state.get("analog_macro_module") or "analog_macro").strip()


def _find_gds(root: str) -> str:
    hits = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".gds"):
                hits.append(os.path.join(dirpath, name))
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "gds")
    os.makedirs(stage_dir, exist_ok=True)

    if not _enabled(state):
        state["analog_gds_generation"] = {"status": "skipped", "reason": "analog_physical_mode_not_generate_sky130_gds"}
        state["status"] = f"{AGENT_NAME}: skipped"
        return state

    module_name = _module_name(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pdk": "sky130A",
        "module": module_name,
        "spice": spice_path,
    }

    if not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "deferred", "reason": "sky130_spice_missing"})
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: deferred"
        return state

    align_bin = shutil.which("schematic2layout.py") or shutil.which("align")
    run_sh = os.path.join(stage_dir, "run_align.sh")
    run_text = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'echo "ChipLoop {AGENT_NAME}"',
        f'echo "SPICE={spice_path}"',
        f'echo "TOP={module_name}"',
        f'echo "PDK=sky130A"',
        "# Expected ALIGN command when available:",
        f"# schematic2layout.py {os.path.abspath(spice_path)} -p sky130 -c {module_name}",
        "",
    ])
    with open(run_sh, "w", encoding="utf-8") as fh:
        fh.write(run_text)
    try:
        os.chmod(run_sh, 0o755)
    except Exception:
        pass

    if not align_bin:
        summary.update({
            "status": "tool_unavailable",
            "reason": "align_not_installed",
            "run_script": run_sh,
            "note": "No GDS was generated. Install ALIGN/schematic2layout.py in the runner to enable analog GDS generation.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_align.sh", run_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: ALIGN unavailable"
        return state

    cmd = [align_bin, os.path.abspath(spice_path), "-p", "sky130", "-c", module_name]
    cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
    log = (cp.stdout or "") + (cp.stderr or "")
    log_path = os.path.join(stage_dir, "align.log")
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(log)

    gds_path = _find_gds(stage_dir)
    if gds_path:
        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
            shutil.copy2(gds_path, final_gds)
        summary.update({"status": "generated", "return_code": cp.returncode, "gds": final_gds, "log": log_path})
        with open(final_gds, "rb") as fh:
            # Store a small text breadcrumb instead of trying to upload binary through text helper.
            pass
        state["analog_macro_gds"] = final_gds
        digital = state.setdefault("digital", {})
        if isinstance(digital, dict):
            digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [final_gds]))
            digital.pop("macro_blackbox_mode", None)
    else:
        summary.update({"status": "failed", "return_code": cp.returncode, "reason": "align_gds_not_produced", "log": log_path})

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_align.sh", run_text)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "align.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
    state["analog_gds_generation"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    return state
