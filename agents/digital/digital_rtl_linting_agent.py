import os
import json
import shutil
import subprocess
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


def _basic_lint_file(path: str) -> List[Dict[str, Any]]:
    issues = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    for i, line in enumerate(lines, start=1):
        if "\t" in line:
            issues.append({"file": path, "line": i, "rule": "NO_TABS", "severity": "warning", "msg": "Tab character found."})
        if len(line.rstrip("\n")) > 140:
            issues.append({"file": path, "line": i, "rule": "LINE_LENGTH", "severity": "info", "msg": "Line > 140 chars."})
        if line.rstrip("\n").endswith(" "):
            issues.append({"file": path, "line": i, "rule": "TRAILING_SPACE", "severity": "info", "msg": "Trailing whitespace."})

    txt = "".join(lines).lower()
    if "initial begin" in txt:
        issues.append({"file": path, "line": None, "rule": "SYNTH_INITIAL",
                       "severity": "warning", "msg": "Found 'initial begin' (may be non-synthesizable depending on target)."})

    return issues


def _try_verilator_lint(rtl_files: List[str], log_path: str) -> Dict[str, Any]:
    if not rtl_files:
        return {"available": False, "reason": "no_rtl_files"}

    if shutil.which("verilator") is None:
        return {"available": False, "reason": "verilator_not_found"}

    cmd = ["verilator", "--lint-only", "-Wall"] + rtl_files
    _log(log_path, f"Running: {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {
            "available": True,
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    except Exception as e:
        return {"available": True, "returncode": -1, "stdout": "", "stderr": f"verilator_run_failed: {e}"}


def run_agent(state: dict) -> dict:
    agent_name = "RTL Linting Agent"
    print("\n🧹 Running RTL Linting Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)

    log_path = os.path.join("artifact", "digital_rtl_linting_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("RTL Linting Agent Log\n")

    rtl_files = _collect_rtl_files(workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files under {workflow_dir}")

    issues = []
    for fpath in rtl_files:
        issues.extend(_basic_lint_file(fpath))

    verilator = _try_verilator_lint(rtl_files, log_path)

    report = {
        "type": "rtl_lint_report",
        "version": "1.0",
        "rtl_file_count": len(rtl_files),
        "heuristic_issues": issues,
        "verilator_lint": {
            "available": verilator.get("available", False),
            "reason": verilator.get("reason"),
            "returncode": verilator.get("returncode"),
        },
        "summary": {
            "heuristic_issue_count": len(issues),
            "recommended_action": "Fix warnings before CDC/reset checks; keep lint clean for better downstream signal inference."
        }
    }

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "rtl_lint_report.json", json.dumps(report, indent=2))
    if verilator.get("available"):
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "verilator_lint_stdout.txt", verilator.get("stdout", ""))
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "verilator_lint_stderr.txt", verilator.get("stderr", ""))

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_rtl_linting_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())

    state["rtl_lint_report_path"] = os.path.join(workflow_dir, "digital", "rtl_lint_report.json")
    return state
