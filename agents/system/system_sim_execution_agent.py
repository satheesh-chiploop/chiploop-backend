
import json
import os
import re
import glob
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

def _now() -> str:
    return datetime.now().isoformat()

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _python_has_module(module_name: str) -> bool:
    try:
        p = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return p.returncode == 0
    except Exception:
        return False

def _has_cocotb_runtime() -> bool:
    if _which("cocotb-config"):
        return True
    if _python_has_module("cocotb") and _python_has_module("cocotb_tools.config"):
        return True
    return False

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _record(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None

def _find_tb_root(workflow_dir: str) -> str:
    p = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(p, exist_ok=True)
    return p

def _find_makefile(tb_root: str) -> Optional[str]:
    p = os.path.join(tb_root, "Makefile")
    return p if os.path.exists(p) else None

def _find_waveforms(search_root: str) -> List[str]:
    pats = ["**/*.vcd", "**/*.fst", "**/*.fsdb"]
    out: List[str] = []
    for pat in pats:
        out.extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    return sorted(list(dict.fromkeys([p for p in out if os.path.isfile(p)])))

def _find_coverage_candidates(search_root: str) -> Dict[str, List[str]]:
    out = {"json": [], "md": [], "dat": [], "logs": []}
    for pat in ["**/*coverage*.json", "**/*functional*.json", "**/*summary*.json"]:
        out["json"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    for pat in ["**/*coverage*.md", "**/COVERAGE.md"]:
        out["md"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    for pat in ["**/*.dat", "**/*coverage*.dat"]:
        out["dat"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    for pat in ["**/*.log", "**/*.txt"]:
        out["logs"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    for k in out:
        out[k] = sorted(list(dict.fromkeys([p for p in out[k] if os.path.isfile(p)])))
    return out

def _count_assertions(workflow_dir: str) -> int:
    total = 0
    for pat in [
        os.path.join(workflow_dir, "vv", "**", "*.sva"),
        os.path.join(workflow_dir, "vv", "**", "assertions.sv"),
        os.path.join(workflow_dir, "vv", "**", "*.sv"),
    ]:
        for p in glob.glob(pat, recursive=True):
            try:
                txt = open(p, "r", encoding="utf-8", errors="ignore").read()
                total += len(re.findall(r"\bassert\s+property\b", txt))
                total += len(re.findall(r"\bassert\s*\(", txt))
            except Exception:
                pass
    return total

def _count_assertion_failures(text: str) -> int:
    cnt = 0
    for line in (text or "").splitlines():
        ll = line.lower()
        if ("assert" in ll or "sva" in ll) and ("fail" in ll or "error" in ll):
            cnt += 1
    return cnt

def _looks_pass(run: Dict[str, Any]) -> bool:
    if run.get("returncode") != 0:
        return False
    combo = ((run.get("stdout") or "") + "\n" + (run.get("stderr") or "")).lower()
    if "traceback" in combo or "assertionerror" in combo:
        return False
    if "failed" in combo and "0 failed" not in combo:
        return False
    if "error" in combo and "0 error" not in combo and "%warning" not in combo:
        return False
    return True

def _run(cmd: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, timeout: int = 1800) -> Dict[str, Any]:
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        dt = round(time.time() - t0, 3)
        return {
            "cmd": cmd,
            "cwd": cwd,
            "returncode": p.returncode,
            "stdout": p.stdout or "",
            "stderr": p.stderr or "",
            "runtime_sec": dt,
        }
    except subprocess.TimeoutExpired as e:
        dt = round(time.time() - t0, 3)
        return {
            "cmd": cmd,
            "cwd": cwd,
            "returncode": -2,
            "stdout": e.stdout or "",
            "stderr": f"TIMEOUT after {timeout}s",
            "runtime_sec": dt,
        }
    except Exception as e:
        dt = round(time.time() - t0, 3)
        return {
            "cmd": cmd,
            "cwd": cwd,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
            "runtime_sec": dt,
        }

def _load_manifest(tb_root: str) -> Dict[str, Any]:
    p = os.path.join(tb_root, "simulation_manifest.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
        except Exception:
            pass
    return {}

def run_agent(state: dict) -> dict:
    agent_name = "System Simulation Execution Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    tb_root = _find_tb_root(workflow_dir)
    reports_dir = os.path.join(tb_root, "reports")
    run_logs_dir = os.path.join(reports_dir, "run_logs")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(run_logs_dir, exist_ok=True)

    out_root = os.path.join(workflow_dir, "system", "sim")
    system_logs_dir = os.path.join(out_root, "logs")
    os.makedirs(out_root, exist_ok=True)
    os.makedirs(system_logs_dir, exist_ok=True)

    log_path = os.path.join("artifact", "system_simulation_execution_agent.log")
    _write(log_path, "System Simulation Execution Agent Log\n")

    makefile = _find_makefile(tb_root)
    if not makefile:
        state["status"] = "❌ Missing vv/tb/Makefile. Run System Testbench Generator Agent first."
        return state
    if not _which("make"):
        state["status"] = "❌ 'make' not found on PATH."
        return state

    manifest = _load_manifest(tb_root)
    top = (
        state.get("soc_top_sim_module")
        or manifest.get("top_module")
        or state.get("top_module")
        or "soc_top_sim"
    )

    tests = (
        state.get("system_sim_testcases")
        or state.get("simulation_testcases")
        or manifest.get("default_tests")
        or ["smoke_test"]
    )
    if isinstance(tests, list):
        normalized_tests: List[str] = []
        for t in tests:
            if isinstance(t, dict):
                name = str(t.get("name") or "").strip()
                if name:
                    normalized_tests.append(name)
            elif isinstance(t, str) and t.strip():
                normalized_tests.append(t.strip())
        tests = normalized_tests or ["smoke_test"]
    else:
        tests = ["smoke_test"]

    seeds = state.get("system_sim_seeds") or state.get("simulation_seeds") or [1]
    if not isinstance(seeds, list):
        seeds = [1]
    num_iters = int(state.get("system_sim_num_iters") or state.get("simulation_num_iters") or 25)

    verilator_present = bool(_which("verilator"))
    cocotb_present = _has_cocotb_runtime()
    if not verilator_present:
        state["status"] = "❌ 'verilator' not found on PATH."
        return state

    if not cocotb_present:
        preflight = {
            "type": "system_sim_execution_preflight",
            "version": "1.0",
            "generated_at": _now(),
            "top_module": top,
            "tools_detected": {
                "make": bool(_which("make")),
                "verilator": verilator_present,
                "cocotb_config": bool(_which("cocotb-config")),
                "python_cocotb": _python_has_module("cocotb"),
                "python_cocotb_tools_config": _python_has_module("cocotb_tools.config"),
                "cocotb_runtime": cocotb_present,
            },
            "status": "failed_preflight",
            "reason": "Neither cocotb-config nor importable cocotb/cocotb_tools.config was found",
            "hint": "Install/activate cocotb in the same backend runtime where System_SIM executes.",
        }
        txt = json.dumps(preflight, indent=2)
        _write(os.path.join(out_root, "system_sim_execution.json"), txt)
        _record(workflow_id, agent_name, "system/sim", "system_sim_execution.json", txt)
        state.setdefault("system_sim", {})
        state["system_sim"]["execution"] = preflight
        state["system_sim_execution"] = preflight
        state["status"] = "❌ System simulation preflight failed: cocotb runtime not found"
        return state

    env_base = os.environ.copy()
    env_base["TOPLEVEL"] = top
    env_base["SIM"] = "verilator"
    env_base["NUM_ITERS"] = str(num_iters)

    extra_args = "--trace --trace-structs --coverage --assert"

    results: List[Dict[str, Any]] = []
    all_waveforms_before = set(_find_waveforms(workflow_dir))
    total_assertions = _count_assertions(workflow_dir)

    for testcase in tests:
        for seed in seeds:
            env = dict(env_base)
            env["RANDOM_SEED"] = str(seed)

            cmd = [
                "make",
                f"TESTCASE={testcase}",
                f"TOPLEVEL={top}",
                "TOPLEVEL_LANG=verilog",
                f"MODULE=test_{top}",
                f"EXTRA_ARGS={extra_args}",
            ]

            run = _run(cmd, cwd=tb_root, env=env, timeout=1800)
            run["testcase"] = testcase
            run["seed"] = seed
            run["top_module"] = top
            run["simulator"] = "verilator"
            run["pass"] = _looks_pass(run)

            stdout_tail = (run.get("stdout") or "").splitlines()[-120:]
            stderr_tail = (run.get("stderr") or "").splitlines()[-120:]
            run["stdout_tail"] = stdout_tail
            run["stderr_tail"] = stderr_tail
            run["assertion_failures"] = _count_assertion_failures((run.get("stdout") or "") + "\n" + (run.get("stderr") or ""))

            base = f"{testcase}__seed_{seed}"
            stdout_path = os.path.join(run_logs_dir, f"{base}.stdout.log")
            stderr_path = os.path.join(run_logs_dir, f"{base}.stderr.log")
            _write(stdout_path, "\n".join(stdout_tail) + "\n")
            _write(stderr_path, "\n".join(stderr_tail) + "\n")

            combined_log_name = f"{testcase}_seed{seed}.log"
            combined_log_path = os.path.join(system_logs_dir, combined_log_name)
            combined_text = (
                f"# cmd: {' '.join(run['cmd'])}\n"
                f"# cwd: {run['cwd']}\n"
                f"# returncode: {run['returncode']}\n"
                f"# runtime_sec: {run['runtime_sec']}\n\n"
                f"===== STDOUT =====\n{run['stdout']}\n\n"
                f"===== STDERR =====\n{run['stderr']}\n"
            )
            _write(combined_log_path, combined_text)
            run["log_path"] = f"system/sim/logs/{combined_log_name}"

            results.append(run)

    all_waveforms_after = set(_find_waveforms(workflow_dir))
    new_waveforms = sorted(list(all_waveforms_after - all_waveforms_before)) or sorted(list(all_waveforms_after))

    tests_passed = sum(1 for r in results if r.get("pass"))
    tests_failed = sum(1 for r in results if not r.get("pass"))
    any_pass = tests_passed > 0

    coverage_candidates = _find_coverage_candidates(workflow_dir) if any_pass else {"json": [], "md": [], "dat": [], "logs": []}

    coverage_json_path = (
        manifest.get("functional_coverage_summary_json")
        or state.get("functional_coverage_summary_json")
        or os.path.join(reports_dir, "functional_coverage_summary.json")
    )
    coverage_md_path = (
        manifest.get("functional_coverage_md")
        or state.get("functional_coverage_md")
        or os.path.join(reports_dir, "COVERAGE.md")
    )
    coverage_json_present = os.path.exists(coverage_json_path)
    coverage_md_present = os.path.exists(coverage_md_path)

    summary = {
        "type": "system_sim_execution",
        "version": "1.1",
        "generated_at": _now(),
        "top_module": top,
        "simulator": "verilator",
        "status": "passed" if tests_failed == 0 else ("partial" if any_pass else "failed"),
        "tools_detected": {
            "make": bool(_which("make")),
            "verilator": verilator_present,
            "cocotb_config": bool(_which("cocotb-config")),
            "python_cocotb": _python_has_module("cocotb"),
            "python_cocotb_tools_config": _python_has_module("cocotb_tools.config"),
            "cocotb_runtime": cocotb_present,
        },
        "matrix": {
            "testcases": tests,
            "seeds": seeds,
            "num_runs": len(tests) * len(seeds),
        },
        "runs": results,
        "tests_run": len(results),
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "total_runtime_sec": round(sum(float(r.get("runtime_sec") or 0.0) for r in results), 3),
        "waveforms": [os.path.relpath(p, workflow_dir).replace("\\", "/") for p in new_waveforms],
        "coverage_candidates": {
            k: [os.path.relpath(p, workflow_dir).replace("\\", "/") for p in v]
            for k, v in coverage_candidates.items()
        } if any_pass else {},
        "functional_coverage_summary_json": coverage_json_path,
        "functional_coverage_md": coverage_md_path,
        "coverage_json_present": coverage_json_present,
        "coverage_md_present": coverage_md_present,
        "assertions_total": total_assertions,
        "assertion_failures_total": sum(int(r.get("assertion_failures") or 0) for r in results),
        "notes": [] if any_pass else ["No simulation run passed; coverage candidates intentionally suppressed."],
    }

    exec_json = json.dumps(summary, indent=2)
    md_lines = [
        "# System Simulation Execution Report",
        "",
        f"- Top: `{top}`",
        f"- Tests run: {summary['tests_run']}",
        f"- Passed: {summary['tests_passed']}",
        f"- Failed: {summary['tests_failed']}",
        f"- Total runtime (s): {summary['total_runtime_sec']}",
        f"- Coverage JSON present: {coverage_json_present}",
        f"- Coverage MD present: {coverage_md_present}",
        "",
        "## Run Matrix",
    ]
    for r in results:
        md_lines.append(
            f"- `{r['testcase']}` / seed `{r['seed']}` → "
            f"{'PASS' if r['pass'] else 'FAIL'} "
            f"(rc={r['returncode']}, {r['runtime_sec']}s)"
        )
    if summary["waveforms"]:
        md_lines.extend(["", "## Waveforms"])
        for w in summary["waveforms"]:
            md_lines.append(f"- `{w}`")
    exec_md = "\n".join(md_lines) + "\n"

    _write(os.path.join(out_root, "system_sim_execution.json"), exec_json)
    _write(os.path.join(out_root, "system_sim_execution.md"), exec_md)

    artifacts = {}
    artifacts["system_sim_execution_json"] = _record(workflow_id, agent_name, "system/sim", "system_sim_execution.json", exec_json)
    artifacts["system_sim_execution_md"] = _record(workflow_id, agent_name, "system/sim", "system_sim_execution.md", exec_md)

    if coverage_json_present:
        try:
            with open(coverage_json_path, "r", encoding="utf-8") as f:
                artifacts["functional_coverage_summary_json"] = _record(
                    workflow_id, agent_name, "vv/tb/reports", "functional_coverage_summary.json", f.read()
                )
        except Exception:
            pass
    if coverage_md_present:
        try:
            with open(coverage_md_path, "r", encoding="utf-8") as f:
                artifacts["functional_coverage_md"] = _record(
                    workflow_id, agent_name, "vv/tb/reports", "COVERAGE.md", f.read()
                )
        except Exception:
            pass

    for r in results:
        base = f"{r['testcase']}__seed_{r['seed']}"
        for p in [
            os.path.join(run_logs_dir, f"{base}.stdout.log"),
            os.path.join(run_logs_dir, f"{base}.stderr.log"),
        ]:
            if os.path.exists(p):
                try:
                    txt = open(p, "r", encoding="utf-8", errors="ignore").read()
                    _record(workflow_id, agent_name, "vv/tb/reports/run_logs", os.path.basename(p), txt)
                except Exception:
                    pass

        p = os.path.join(system_logs_dir, f"{r['testcase']}_seed{r['seed']}.log")
        if os.path.exists(p):
            try:
                txt = open(p, "r", encoding="utf-8", errors="ignore").read()
                _record(workflow_id, agent_name, "system/sim/logs", os.path.basename(p), txt)
            except Exception:
                pass

    _record(workflow_id, agent_name, "vv", "system_simulation_execution_agent.log", open(log_path, "r", encoding="utf-8").read())

    state.setdefault("system_sim", {})
    state["system_sim"]["execution"] = summary
    state["system_sim_execution"] = summary
    state["simulation_execution_summary_json"] = os.path.join(out_root, "system_sim_execution.json")
    state["status"] = f"✅ System simulation executed: {tests_passed}/{len(results)} runs passed"
    return state
