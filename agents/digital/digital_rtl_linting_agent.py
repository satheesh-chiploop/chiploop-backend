import os
import json
from datetime import datetime
from typing import List, Dict, Any

from tooling.profiles import profile_summary
from tooling.runner import run_tool, tool_available
from utils.artifact_utils import save_text_artifact_and_record

SYNTHESIS_BLOCKING_VERILATOR_WARNINGS = {
    "PINMISSING",
    "UNDRIVEN",
    "MULTIDRIVEN",
}


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


def _rtl_files_from_state(state: Dict[str, Any], workflow_dir: str) -> List[str]:
    candidates: List[str] = []
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    for value in (
        digital.get("rtl_files"),
        state.get("rtl_files"),
        state.get("system_rtl_files"),
        state.get("rtl_inputs"),
        state.get("source_rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend(str(path) for path in value if str(path).strip())
    out = []
    for raw in candidates:
        path = raw if os.path.isabs(raw) else os.path.join(workflow_dir, raw)
        path = os.path.abspath(path)
        low = path.replace("\\", "/").lower()
        if not path.lower().endswith((".v", ".sv")):
            continue
        if any(skip in low for skip in ("/vv/", "/tb/", "/testbench/", "/formal/")):
            continue
        if os.path.exists(path):
            out.append(path)
    return sorted(dict.fromkeys(out))


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


def _try_verilator_lint(rtl_files: List[str], log_path: str, state: Dict[str, Any]) -> Dict[str, Any]:
    if not rtl_files:
        return {"available": False, "reason": "no_rtl_files"}

    if not tool_available("verilator", state):
        return {"available": False, "reason": "verilator_not_found"}

    args = ["--lint-only", "-Wall", "-Wno-fatal"] + rtl_files
    _log(log_path, f"Running via ChipLoop tool profile: verilator {' '.join(args)}")
    try:
        p = run_tool(
            state,
            "rtl_lint",
            "verilator",
            args,
            timeout_sec=300,
            metadata={"agent": "RTL Linting Agent"},
        )
        return {
            "available": True,
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "run_result": p.to_dict(),
        }
    except Exception as e:
        return {"available": True, "returncode": -1, "stdout": "", "stderr": f"verilator_run_failed: {e}"}


def _verilator_warning_codes(stderr: str) -> List[str]:
    codes = []
    for line in (stderr or "").splitlines():
        marker = "%Warning-"
        if marker not in line:
            continue
        code = line.split(marker, 1)[1].split(":", 1)[0].strip()
        if code and code not in codes:
            codes.append(code)
    return codes


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

    rtl_files = _rtl_files_from_state(state, workflow_dir) or _collect_rtl_files(workflow_dir)
    _log(log_path, f"Discovered {len(rtl_files)} RTL files under {workflow_dir}")

    issues = []
    for fpath in rtl_files:
        issues.extend(_basic_lint_file(fpath))

    verilator = _try_verilator_lint(rtl_files, log_path, state)
    warning_codes = _verilator_warning_codes(str(verilator.get("stderr") or ""))
    blocking_warning_codes = [
        code for code in warning_codes if code in SYNTHESIS_BLOCKING_VERILATOR_WARNINGS
    ]
    verilator_failed = bool(verilator.get("available")) and int(verilator.get("returncode") or 0) != 0
    structural_lint_failed = verilator_failed or bool(blocking_warning_codes)

    report = {
        "type": "rtl_lint_report",
        "version": "1.0",
        "status": "fail" if structural_lint_failed else ("warn" if warning_codes or issues else "pass"),
        "rtl_file_count": len(rtl_files),
        "heuristic_issues": issues,
        "verilator_lint": {
            "available": verilator.get("available", False),
            "reason": verilator.get("reason"),
            "returncode": verilator.get("returncode"),
            "warning_codes": warning_codes,
            "blocking_warning_codes": blocking_warning_codes,
        },
        "summary": {
            "heuristic_issue_count": len(issues),
            "verilator_warning_count": len(warning_codes),
            "blocking_verilator_warning_count": len(blocking_warning_codes),
            "recommended_action": "Fix warnings before CDC/reset checks; keep lint clean for better downstream signal inference."
        },
        "tooling": {
            "tool_profile": profile_summary(state),
            "executions": [verilator.get("run_result")] if verilator.get("run_result") else [],
        },
    }

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "rtl_lint_report.json", json.dumps(report, indent=2))
    if verilator.get("available"):
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "verilator_lint_stdout.txt", verilator.get("stdout", ""))
        save_text_artifact_and_record(workflow_id, agent_name, "digital", "verilator_lint_stderr.txt", verilator.get("stderr", ""))

    save_text_artifact_and_record(workflow_id, agent_name, "digital", "digital_rtl_linting_agent.log",
                                  open(log_path, "r", encoding="utf-8").read())
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "digital",
        "tool_profile_used.json",
        json.dumps(profile_summary(state), indent=2),
    )
    save_text_artifact_and_record(
        workflow_id,
        agent_name,
        "digital",
        "tool_execution_summary.json",
        json.dumps(report["tooling"], indent=2),
    )

    state["rtl_lint_report_path"] = os.path.join(workflow_dir, "digital", "rtl_lint_report.json")
    local_report_path = state["rtl_lint_report_path"]
    os.makedirs(os.path.dirname(local_report_path), exist_ok=True)
    with open(local_report_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))
    state["tool_profile"] = profile_summary(state)
    return state
