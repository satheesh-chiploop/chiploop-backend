import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .fpga_common import fpga_dir, manifest_update, publish_json, read_text, write_text


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: List[str], cwd: str, log_path: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
        write_text(log_path, proc.stdout or "")
        return {"available": True, "cmd": cmd, "returncode": proc.returncode, "ok": proc.returncode == 0, "log": log_path}
    except FileNotFoundError:
        write_text(log_path, f"Tool not found: {cmd[0]}\n")
        return {"available": False, "cmd": cmd, "returncode": 127, "ok": False, "log": log_path}
    except Exception as exc:
        write_text(log_path, f"{type(exc).__name__}: {exc}\n")
        return {"available": True, "cmd": cmd, "returncode": -1, "ok": False, "log": log_path, "error": str(exc)}


def _lint_pass(state: dict, rtl_files: List[str], out_dir: str, suffix: str) -> Dict[str, Any]:
    results: Dict[str, Any] = {"suffix": suffix, "rtl_file_count": len(rtl_files), "tools": {}}
    if _tool("iverilog"):
        results["tools"]["iverilog"] = _run(
            ["iverilog", "-g2012", "-tnull", *rtl_files],
            cwd=out_dir,
            log_path=os.path.abspath(f"{out_dir}/iverilog_{suffix}.log"),
        )
    else:
        results["tools"]["iverilog"] = {"available": False, "ok": False, "reason": "iverilog_not_found"}
    if _tool("verilator"):
        results["tools"]["verilator"] = _run(
            ["verilator", "--lint-only", "-Wall", "-Wno-fatal", *rtl_files],
            cwd=out_dir,
            log_path=os.path.abspath(f"{out_dir}/verilator_{suffix}.log"),
        )
    else:
        results["tools"]["verilator"] = {"available": False, "ok": False, "reason": "verilator_not_found"}

    available = [tool for tool in results["tools"].values() if tool.get("available")]
    results["status"] = "pass" if available and all(tool.get("ok") for tool in available) else "fail"
    if not available:
        results["status"] = "not_available"
    return results


def _safe_repair_text(text: str) -> str:
    lines = []
    blank_count = 0
    for line in text.splitlines():
        cleaned = line.rstrip().replace("\t", "    ")
        if not cleaned:
            blank_count += 1
            if blank_count > 2:
                continue
        else:
            blank_count = 0
        lines.append(cleaned)
    repaired = "\n".join(lines).strip() + "\n"
    repaired = re.sub(r"\bendmodule\s+endmodule\b", "endmodule", repaired)
    return repaired


def _repair_files(rtl_files: List[str], out_dir: str) -> Dict[str, Any]:
    repaired_dir = os.path.abspath(f"{out_dir}/repaired")
    os.makedirs(repaired_dir, exist_ok=True)
    repaired_files: List[str] = []
    changed = 0
    for index, path in enumerate(rtl_files):
        text = read_text(path)
        repaired = _safe_repair_text(text)
        name = os.path.basename(path) or f"rtl_{index}.sv"
        out_path = os.path.abspath(f"{repaired_dir}/{name}")
        Path(out_path).write_text(repaired, encoding="utf-8")
        repaired_files.append(out_path)
        if repaired != text:
            changed += 1
    return {
        "mode": "safe_format_and_syntax_cleanup",
        "semantic_changes_allowed": False,
        "files_changed": changed,
        "rtl_files": repaired_files,
    }


def run_agent(state: dict) -> dict:
    agent = "FPGA RTL Quality Gate Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "quality")
    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    repair_enabled = bool(state.get("run_fpga_rtl_repair_loop", True))
    summary: Dict[str, Any] = {
        "agent": agent,
        "status": "blocked",
        "repair_enabled": repair_enabled,
        "rtl_file_count": len(rtl_files),
    }
    if not rtl_files:
        summary["error"] = "No RTL files available for FPGA quality gate."
    else:
        pass1 = _lint_pass(state, rtl_files, out_dir, "pass1")
        summary["pass1"] = pass1
        final_files = rtl_files
        repair = {"enabled": repair_enabled, "applied": False}
        if repair_enabled and pass1.get("status") != "pass":
            repair.update(_repair_files(rtl_files, out_dir))
            repair["applied"] = True
            final_files = repair.get("rtl_files") or rtl_files
            manifest_update(state, "rtl_files", final_files)
        summary["repair"] = repair
        pass2 = _lint_pass(state, final_files, out_dir, "pass2")
        summary["pass2"] = pass2
        summary["status"] = "pass" if pass2.get("status") == "pass" else pass2.get("status", "fail")
        summary["rtl_files"] = final_files

    publish_json(state, agent, "quality", "fpga_rtl_quality_summary.json", summary)
    manifest_update(state, "rtl_quality", summary)
    if summary["status"] == "fail":
        state["status"] = "FPGA RTL quality gate failed before synthesis."
        raise RuntimeError(state["status"])
    return state
