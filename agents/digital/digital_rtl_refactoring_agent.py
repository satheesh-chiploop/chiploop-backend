import os
import json
from datetime import datetime
from typing import List

from utils.artifact_utils import save_text_artifact_and_record


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _collect_rtl_files_from_state_or_workflow(state: dict, workflow_dir: str) -> List[str]:
    files = []
    artifact_list = state.get("artifact_list") or []
    if isinstance(artifact_list, list):
        for p in artifact_list:
            if isinstance(p, str) and os.path.exists(p) and p.endswith((".v", ".sv")):
                files.append(p)

    if files:
        return sorted(list(dict.fromkeys(files)))

    rtl = []
    for root, _, fs in os.walk(workflow_dir):
        for fn in fs:
            if fn.endswith((".v", ".sv")):
                rtl.append(os.path.join(root, fn))
    return sorted(rtl)


def _format_only_cleanup(text: str) -> str:
    lines = text.splitlines()
    out = []
    blank_count = 0

    for ln in lines:
        ln = ln.rstrip()
        ln = ln.replace("\t", "    ")

        if ln == "":
            blank_count += 1
            if blank_count > 2:
                continue
        else:
            blank_count = 0

        out.append(ln)

    return "\n".join(out).strip() + "\n"


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

    rtl_files = _collect_rtl_files_from_state_or_workflow(state, workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files.")

    if not rtl_files:
        _log(log_path, "No RTL files found. Nothing to refactor.")
        state["status"] = "⚠ No RTL files found for refactoring."
        state["rtl_refactor_plan_path"] = None
        return state

    refactor_plan = {
        "type": "rtl_refactor_plan",
        "version": "1.0",
        "mode": "format_only",
        "rules": {
            "semantic_changes_allowed": False,
            "interface_changes_allowed": False,
            "behavior_changes_allowed": False,
        },
        "goals": [
            "Improve readability and consistency",
            "Preserve synthesizable semantics",
            "Preserve interfaces exactly",
        ],
        "safe_actions_applied": [
            "Remove trailing whitespace",
            "Convert tabs to spaces",
            "Collapse excessive blank lines",
            "Ensure file ends with newline",
        ],
        "future_recommendations": [
            "Keep datapath/control separation clear in RTL generator instead of post-editing.",
            "Standardize always block templates in RTL generation stage.",
            "Preserve module headers exactly as emitted by RTL agent.",
        ],
        "files_processed": [],
    }

    plan_path = os.path.join(workflow_dir, "digital", "rtl_refactor_plan.json")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)

    refactored_paths = []
    for fp in rtl_files:
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()

        cleaned = _format_only_cleanup(raw)
        base = os.path.basename(fp)
        out_path = os.path.join(workflow_dir, "digital", "rtl_refactored", base)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(cleaned)

        refactored_paths.append(out_path)

        try:
            save_text_artifact_and_record(
                workflow_id,
                agent_name,
                "digital/rtl_refactored",
                base,
                cleaned
            )
        except Exception as e:
            _log(log_path, f"Artifact upload warning for {base}: {e}")

        refactor_plan["files_processed"].append({
            "input": fp,
            "output": out_path,
            "artifact": f"digital/rtl_refactored/{base}",
        })

    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(refactor_plan, f, indent=2)

    try:
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "digital",
            "rtl_refactor_plan.json",
            open(plan_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "digital",
            "digital_rtl_refactoring_agent.log",
            open(log_path, "r", encoding="utf-8").read()
        )
    except Exception as e:
        _log(log_path, f"Plan/log artifact upload warning: {e}")

    state["status"] = "✅ RTL refactoring completed."
    state["rtl_refactor_plan_path"] = plan_path
    state["rtl_refactored_files"] = refactored_paths
    return state