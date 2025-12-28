import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any

from utils.artifact_utils import save_text_artifact_and_record


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _collect_rtl_files(workflow_dir: str) -> List[str]:
    rtl = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.endswith((".v", ".sv")):
                rtl.append(os.path.join(root, fn))
    return sorted(rtl)


def _format_only_cleanup(text: str) -> str:
    # Safe cleanup: normalize whitespace, remove trailing spaces, preserve semantics.
    lines = text.splitlines()
    out = []
    for ln in lines:
        ln = ln.rstrip()
        ln = ln.replace("\t", "    ")
        out.append(ln)
    return "\n".join(out) + "\n"


def run_agent(state: dict) -> dict:
    agent_name = "RTL Refactoring Agent"
    print("\n🧱 Running RTL Refactoring Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    log_path = os.path.join("artifact", "digital_rtl_refactoring_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("RTL Refactoring Agent Log\n")

    rtl_files = _collect_rtl_files(workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files.")

    refactor_plan = {
        "type": "rtl_refactor_plan",
        "version": "1.0",
        "goals": [
            "Improve readability and consistency",
            "Preserve synthesizable semantics",
            "Reduce duplication where obvious (follow-up enhancement)",
        ],
        "safe_actions_applied": [
            "Remove trailing whitespace",
            "Convert tabs to spaces",
            "Ensure file ends with newline",
        ],
        "future_recommendations": [
            "Modularize repeated combinational logic into functions/tasks (SystemVerilog) or localparams.",
            "Separate datapath vs control blocks and add clear comments per section.",
            "Standardize reset style and always block templates across modules.",
        ],
        "files_processed": [],
    }

    # Emit cleaned copies in artifacts (does NOT overwrite original in repo)
    for fp in rtl_files:
        raw = open(fp, "r", encoding="utf-8", errors="ignore").read()
        cleaned = _format_only_cleanup(raw)
        base = os.path.basename(fp)
        out_name = f"refactored_{base}"
        save_text_artifact_and_record(workflow_id, agent_name, "digital/rtl_refactored", out_name, cleaned)
        refactor_plan["files_processed"].append({"input": fp, "artifact": f"digital/rtl_refactored/{out_name}"})

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "rtl_refactor_plan.json", json.dumps(refactor_plan, indent=2))
    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_rtl_refactoring_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["rtl_refactor_plan_path"] = os.path.join(workflow_dir, "digital", "rtl_refactor_plan.json")
    return state
