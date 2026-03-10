import os
import re
import json
import glob
import time
import shutil
import subprocess
import sys
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
    # Accept either cocotb-config on PATH or importable cocotb + cocotb_tools.config
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


def _find_tb_root(workflow_dir: str) -> str:
    cand = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(cand, exist_ok=True)
    return cand


def _find_makefile(tb_root: str) -> Optional[str]:
    p = os.path.join(tb_root, "Makefile")
    return p if os.path.exists(p) else None


def _find_waveforms(search_root: str) -> List[str]:
    pats = ["**/*.vcd", "**/*.fst", "**/*.fsdb"]
    out: List[str] = []
    for pat in pats:
        out.extend(glob.glob(os.path.join(search_root, pat), recursive=True))
    out = sorted(list(dict.fromkeys([p for p in out if os.path.isfile(p)])))
    return out


def _find_coverage_candidates(search_root: str) -> Dict[str, List[str]]:
    out = {
        "json": [],
        "md": [],
        "dat": [],
        "logs": [],
    }

    for pat in [
        "**/*coverage*.json",
        "**/*functional*.json",
        "**/*summary*.json",
    ]:
        out["json"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))

    for pat in [
        "**/*coverage*.md",
        "**/COVERAGE.md",
    ]:
        out["md"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))

    for pat in [
        "**/*.dat",
        "**/*coverage*.dat",
    ]:
        out["dat"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))

    for pat in [
        "**/*.log",
        "**/*.txt",
    ]:
        out["logs"].extend(glob.glob(os.path.join(search_root, pat), recursive=True))

    for k in out:
        out[k] = sorted(list(dict.fromkeys([p for p in out[k] if os.path.isfile(p)])))
    return out


def _count_assertions(workflow_dir: str) -> int:
    total = 0
    for pat in [
        os.path.join(workflow_dir, "vv", "**", "*.sva"),
        os.path.join(workflow_dir, "vv", "**", "assertions.sv"),
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
    pats = [
        r"assert",
        r"Assertion",
        r"%Error",
        r"failed",
    ]
    # best-effort: count lines that mention both assertion-ish and failure-ish tokens
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
    if "traceback" in combo:
        return False
    if "assertionerror" in combo:
        return False
    if "failed" in combo and "0 failed" not in combo:
        return False
    if "error" in combo and "0 error" not in combo and "%warning" not in combo:
        return False
    return True

def run_agent(state: dict) -> dict:
    agent_name = "System Simulation Execution Agent"
    print("\n🚀 Running System Simulation Execution Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    tb_root = _find_tb_root(workflow_dir)
    makefile = _find_makefile(tb_root)

    if not makefile:
        state["status"] = "❌ Missing vv/tb/Makefile. Run Digital Testbench Generator Agent first."
        return state

    if not _which("make"):
        state["status"] = "❌ 'make' not found on PATH."
        return state

    top = (
        state.get("soc_top_sim_module")
        or state.get("top_module")
        or "soc_top_sim"
    )

    # Demo defaults: 2 testcases × 2 seeds
    testcases = state.get("system_sim_testcases") or ["smoke_test", "constrained_random_sanity"]
    seeds = state.get("system_sim_seeds") or [1, 2]
    num_iters = int(state.get("system_sim_num_iters") or 25)

    out_root = os.path.join(workflow_dir, "system", "sim")
    logs_root = os.path.join(out_root, "logs")
    os.makedirs(logs_root, exist_ok=True)

    verilator_present = bool(_which("verilator"))
    cocotb_present = _has_cocotb_runtime()

    if not verilator_present:
        state["status"] = "❌ 'verilator' not found on PATH."
        return state

    if not cocotb_present:
        # Persist a clean preflight artifact instead of letting every make run fail noisily.
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

    # Force coverage/assert switches through make variable override.
    # This works with the generated Makefile because it already passes EXTRA_ARGS through to the simulator build flow. :contentReference[oaicite:1]{index=1}
    extra_args = "--trace --trace-structs --coverage --assert"

    runs: List[Dict[str, Any]] = []
    all_waveforms_before = set(_find_waveforms(workflow_dir))
    total_assertions = _count_assertions(workflow_dir)

    for testcase in testcases:
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

            result = _run(cmd, cwd=tb_root, env=env, timeout=1800)
            result["testcase"] = testcase
            result["seed"] = seed
            result["top_module"] = top
            result["simulator"] = "verilator"
            result["pass"] = _looks_pass(result)

            log_name = f"{testcase}_seed{seed}.log"
            log_text = (
                f"# cmd: {' '.join(result['cmd'])}\n"
                f"# cwd: {result['cwd']}\n"
                f"# returncode: {result['returncode']}\n"
                f"# runtime_sec: {result['runtime_sec']}\n\n"
                f"===== STDOUT =====\n{result['stdout']}\n\n"
                f"===== STDERR =====\n{result['stderr']}\n"
            )
            _write(os.path.join(logs_root, log_name), log_text)

            result["assertion_failures"] = _count_assertion_failures(log_text)
            result["log_path"] = f"system/sim/logs/{log_name}"
            runs.append(result)

    

    all_waveforms_after = set(_find_waveforms(workflow_dir))
    new_waveforms = sorted(list(all_waveforms_after - all_waveforms_before))
    if not new_waveforms:
        new_waveforms = sorted(list(all_waveforms_after))

    any_pass = any(r.get("pass") for r in runs)
    coverage_candidates = _find_coverage_candidates(workflow_dir) if any_pass else {
        "json": [],
        "md": [],
        "dat": [],
        "logs": [],
    }
    tests_passed = sum(1 for r in runs if r.get("pass"))
    tests_failed = sum(1 for r in runs if not r.get("pass"))
    any_pass = tests_passed > 0

    summary = {
        "type": "system_sim_execution",
        "version": "1.0",
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
            "testcases": testcases,
            "seeds": seeds,
            "num_runs": len(testcases) * len(seeds),
        },
        "runs": runs,
        "tests_run": len(runs),
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "total_runtime_sec": round(sum(float(r.get("runtime_sec") or 0.0) for r in runs), 3),
        "waveforms": [os.path.relpath(p, workflow_dir).replace("\\", "/") for p in new_waveforms],
        "coverage_candidates": {
            k: [os.path.relpath(p, workflow_dir).replace("\\", "/") for p in v]
            for k, v in coverage_candidates.items()
        } if any_pass else {},
        "assertions_total": total_assertions,
        "assertion_failures_total": sum(int(r.get("assertion_failures") or 0) for r in runs),
        "notes": [] if any_pass else [
            "No simulation run passed; coverage candidates intentionally suppressed."
        ],
    }

    

    # Persist summary
    exec_json = json.dumps(summary, indent=2)
    md_lines = [
        "# System Simulation Execution Report",
        "",
        f"- Top: `{top}`",
        f"- Tests run: {summary['tests_run']}",
        f"- Passed: {summary['tests_passed']}",
        f"- Failed: {summary['tests_failed']}",
        f"- Total runtime (s): {summary['total_runtime_sec']}",
        "",
        "## Run Matrix",
    ]
    for r in runs:
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

    _record(workflow_id, agent_name, "system/sim", "system_sim_execution.json", exec_json)
    _record(workflow_id, agent_name, "system/sim", "system_sim_execution.md", exec_md)

    for r in runs:
        p = os.path.join(logs_root, f"{r['testcase']}_seed{r['seed']}.log")
        if os.path.exists(p):
            try:
                txt = open(p, "r", encoding="utf-8", errors="ignore").read()
                _record(workflow_id, agent_name, "system/sim/logs", os.path.basename(p), txt)
            except Exception:
                pass

    state.setdefault("system_sim", {})
    state["system_sim"]["execution"] = summary
    state["system_sim_execution"] = summary
    state["status"] = (
        f"✅ System simulation executed: "
        f"{summary['tests_passed']}/{summary['tests_run']} runs passed"
    )
    return state