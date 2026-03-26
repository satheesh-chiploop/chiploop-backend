"""
ChipLoop Verification & Validation Agents (Cocotb + Verilator + SymbiYosys)

Design goals:
- No hardcoding of clocks/resets/ports: derive from `digital_spec_json` (Digital Spec Agent output)
- Be regression-friendly: Verilator + Cocotb, reproducible seeds
- Keep artifacts small & useful: generate drop-in files + a concise JSON report
- Fail gracefully if tools are missing (still generate configs/templates)

State contract (expected keys, but all optional):
- workflow_id: str
- workflow_dir: str (default: backend/workflows/<workflow_id>)
- spec_json: str (path to Digital Spec Agent JSON; default: <workflow_dir>/digital/spec.json)
- rtl_files: list[str] optional; otherwise we scan workflow_dir for .v/.sv
- top_module: str optional; otherwise from spec.top_module.name or inferred
- dut_lang: "verilog"|"systemverilog" optional
"""

import os
import re
import json
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()

def _log(path: str, msg: str) -> None:
    print(msg)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] {msg}\n")

def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir

def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")

    handoff_dirs = [
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "rtl"),
        os.path.join(workflow_dir, "handoff", "rtl"),
    ]

    for d in handoff_dirs:
        if not os.path.isdir(d):
            continue

        rtl: List[str] = []
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))

        rtl = sorted(set(rtl))
        if rtl:
            return rtl

    return []

def _pick_top_module(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    if state_top:
        return state_top
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()
    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return "top"

def _ports_from_spec(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    tm = spec.get("top_module") or {}
    ports = tm.get("ports")
    if isinstance(ports, list) and ports:
        return ports
    ports = spec.get("ports")
    if isinstance(ports, list):
        return ports
    io = spec.get("io")
    out: List[Dict[str, Any]] = []
    if isinstance(io, dict):
        for dkey, direction in [("inputs", "input"), ("outputs", "output"), ("inouts", "inout")]:
            arr = io.get(dkey)
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, dict) and p.get("name"):
                        q = dict(p)
                        q.setdefault("direction", direction)
                        out.append(q)
    return out

def _infer_clocks_resets(spec: Dict[str, Any], ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    clk_spec = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks")
    if isinstance(clk_spec, list):
        for c in clk_spec:
            if isinstance(c, dict) and c.get("name"):
                clocks.append(c["name"])
            elif isinstance(c, str):
                clocks.append(c)

    rst_spec = spec.get("resets") or (spec.get("reset") or {})
    if isinstance(rst_spec, list):
        for r in rst_spec:
            if isinstance(r, dict) and r.get("name"):
                resets.append(r)
            elif isinstance(r, str):
                resets.append({"name": r})
    elif isinstance(rst_spec, dict):
        if rst_spec.get("name"):
            resets.append(rst_spec)

    if not clocks:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"\b(clk|clock)\b", nm, re.IGNORECASE):
                clocks.append(nm)

    if not resets:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"\b(rst|reset)\b", nm, re.IGNORECASE):
                resets.append({"name": nm})

    clocks = [c for c in clocks if isinstance(c, str) and c.strip()]
    clocks = list(dict.fromkeys(clocks))

    norm_resets: List[Dict[str, Any]] = []
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            rr = {
                "name": r.get("name"),
                "active_low": bool(r.get("active_low", False) or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")),
                "async": bool(str(r.get("type", "")).lower() in ("async", "asynchronous") or r.get("async", False)),
            }
            norm_resets.append(rr)

    seen = set()
    uniq: List[Dict[str, Any]] = []
    for rr in norm_resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq.append(rr)

    return clocks, uniq

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _record_text(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
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

def _gen_regression_runner(top: str, default_tests: list) -> str:
    return f'''"""Regression runner for Cocotb + Verilator."""

import argparse
import json
import os
import subprocess
from datetime import datetime

DEFAULT_TESTS = {json.dumps(default_tests, indent=2)}

def _now():
    return datetime.now().isoformat()

def run_one(testcase: str, seed: int) -> dict:
    env = os.environ.copy()
    env["RANDOM_SEED"] = str(seed)
    cmd = ["make", f"TESTCASE={{testcase}}"]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return {{
        "testcase": testcase,
        "seed": seed,
        "ok": p.returncode == 0,
        "returncode": p.returncode,
        "stdout_tail": (p.stdout or "").splitlines()[-80:],
        "stderr_tail": (p.stderr or "").splitlines()[-80:],
    }}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", nargs="+", default=DEFAULT_TESTS)
    ap.add_argument("--seeds", nargs="+", type=int, default=[1])
    ap.add_argument("--out", default="reports/regression_summary.json")
    args = ap.parse_args()

    results = []
    for t in args.tests:
        for s in args.seeds:
            results.append(run_one(t, s))

    out = {{
        "type": "simulation_regression",
        "top_module": "{top}",
        "generated_at": _now(),
        "default_tests": DEFAULT_TESTS,
        "results": results,
        "pass_count": sum(1 for r in results if r["ok"]),
        "fail_count": sum(1 for r in results if not r["ok"]),
    }}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    raise SystemExit(0 if out["fail_count"] == 0 else 2)

if __name__ == "__main__":
    main()
'''


def _gen_simulation_manifest(
    top: str,
    spec_path: str,
    rtl_files: List[str],
    default_tests: List[str],
    tb_root: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "vv_simulation_manifest",
        "version": "1.1",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_files": rtl_files,
        "default_tests": default_tests,
        "runner": os.path.join(tb_root, "run_regression.py"),
        "makefile": os.path.join(tb_root, "Makefile"),
        "testcases_json": os.path.join(tb_root, "testcases.json"),
        "tb_contract_json": os.path.join(tb_root, "tb_contract.json"),
        "reports_dir": os.path.join(tb_root, "reports"),
        "simulator": "verilator",
        "sva_assertions_path": state.get("sva_assertions_path"),
        "sva_bind_path": state.get("sva_bind_path"),
        "coverage_model_py": state.get("coverage_model_py"),
        "functional_coverage_summary_json": state.get("functional_coverage_summary_json"),
        "functional_coverage_md": state.get("functional_coverage_md"),
    }

def run_agent(state: dict) -> dict:
    agent_name = "Digital Simulation Control Agent"
    print("\n🎛️ Running Digital Simulation Control Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "simulation_control_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Simulation Control Agent Log\n")

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _safe_read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    rtl_files = [os.path.abspath(p) for p in rtl_files if isinstance(p, str)]
    if not rtl_files:
        raise FileNotFoundError("No handoff RTL files found for simulation control")
    top = _pick_top_module(spec, rtl_files, state.get("top_module"))

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    default_tests = state.get("vv_testcases") or ["smoke_test", "constrained_random_sanity"]
    state["vv_testcases"] = default_tests
    state["testcases"] = default_tests

    runner_py = _gen_regression_runner(top, default_tests)
    _write_file(os.path.join(tb_root, "run_regression.py"), runner_py)

    artifacts: Dict[str, Any] = {}
    sim_manifest = _gen_simulation_manifest(
        top=top,
        spec_path=spec_path,
        rtl_files=rtl_files,
        default_tests=default_tests,
        tb_root=tb_root,
        state=state,
    )
    sim_manifest_txt = json.dumps(sim_manifest, indent=2)
    sim_manifest_path = os.path.join(tb_root, "simulation_manifest.json")
    _write_file(sim_manifest_path, sim_manifest_txt)

    artifacts["simulation_manifest"] = _record_text(
        workflow_id,
        agent_name,
        "vv/tb",
        "simulation_manifest.json",
        sim_manifest_txt,
    )


    readme = """# Simulation Control

Generated:
- `run_regression.py` : orchestrates multi-seed runs via `make TESTCASE=...`

Seed management:
- override with `RANDOM_SEED=<int>`

Usage:
```bash
python run_regression.py --tests smoke_test constrained_random_sanity --seeds 1 2 3 4 5
```
"""
    _write_file(os.path.join(tb_root, "SIM_CONTROL.md"), readme)


    artifacts["runner_py"] = _record_text(workflow_id, agent_name, "vv/tb", "run_regression.py", runner_py)
    artifacts["sim_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "SIM_CONTROL.md", readme)
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "simulation_control_agent.log", open(log_path, "r", encoding="utf-8").read())

    report = {
        "type": "vv_simulation_control_generation",
        "version": "1.1",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "default_tests": default_tests,
        "simulation_manifest_json": sim_manifest_path,
        "tools_detected": {
            "verilator": bool(_which("verilator")),
            "make": bool(_which("make"))
        },
        "artifacts": artifacts,
    }

    
    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_root, "sim_control_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "sim_control_generation_report.json", rep_txt)

    state.setdefault("vv", {})
    state["vv"]["sim_control"] = report
    state["simulation_manifest_json"] = sim_manifest_path
    state["simulation_runner_py"] = os.path.join(tb_root, "run_regression.py")
    return state
