"""
ChipLoop Verification & Validation - System Simulation Control Agent

Design goals:
- dedicated to system-level simulation collateral; no digital/system mode switching
- reuse the stabilized digital simulation-control structure with minimal changes
- treat system integration outputs as the primary source of truth for DUT + RTL set
- pick up generated system testbench / SVA / coverage collateral when present
- keep artifacts small, deterministic, and usable even when tools are not installed

Expected state inputs (all optional):
- workflow_id: str
- workflow_dir: str (default backend/workflows/<workflow_id>)
- system_integration_intent_json: str
- system_top_sim_path / soc_top_sim_path: str
- system_rtl_filelist_sim: list[str] or path to txt file
- system_rtl_files / rtl_inputs / rtl_files: list[str]
- top_module / soc_top_sim_module: str
- tb_makefile: str
- tb_test_py: str
- tb_testcases_json: str
- tb_contract_json: str
- sva_assertions_path: str
- sva_bind_path: str
- coverage_model_py: str
- functional_coverage_summary_json: str
- functional_coverage_md: str
"""

import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record


def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str) -> None:
    print(msg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] {msg}\n")


def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}


def _safe_read_lines(path: Optional[str]) -> List[str]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    return []


def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir


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


def _find_first_existing(candidates: List[Optional[str]]) -> Optional[str]:
    for p in candidates:
        if p and isinstance(p, str) and os.path.exists(p):
            return p
    return None


def _find_system_integration_json(workflow_dir: str) -> Optional[str]:
    return _find_first_existing([
        os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json"),
        os.path.join(workflow_dir, "system_integration_intent.json"),
    ])


def _find_soc_top_sim_path(workflow_dir: str) -> Optional[str]:
    return _find_first_existing([
        os.path.join(workflow_dir, "system", "integration", "soc_top_sim.sv"),
        os.path.join(workflow_dir, "soc_top_sim.sv"),
    ])


def _find_system_rtl_filelist(workflow_dir: str) -> Optional[str]:
    return _find_first_existing([
        os.path.join(workflow_dir, "system", "integration", "system_rtl_filelist_sim.txt"),
        os.path.join(workflow_dir, "system_rtl_filelist_sim.txt"),
    ])


def _find_regmap_json(workflow_dir: str) -> Optional[str]:
    return _find_first_existing([
        os.path.join(workflow_dir, "digital", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "digital_regmap.json"),
    ])


def _normalize_path_list(items: Any) -> List[str]:
    out: List[str] = []
    if isinstance(items, list):
        for p in items:
            if isinstance(p, str) and p.strip():
                out.append(os.path.abspath(p.strip()))
    elif isinstance(items, str) and items.strip():
        if os.path.exists(items):
            for ln in _safe_read_lines(items):
                out.append(os.path.abspath(ln))
        else:
            out.append(os.path.abspath(items.strip()))
    return list(dict.fromkeys(out))


def _rel_to_workflow(workflow_dir: str, path: Optional[str]) -> Optional[str]:
    if not path or not isinstance(path, str):
        return path
    try:
        return os.path.relpath(path, workflow_dir).replace("\\", "/")
    except Exception:
        return path.replace("\\", "/")


def _rel_list_to_workflow(workflow_dir: str, paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths or []:
        if isinstance(p, str) and p.strip():
            out.append(_rel_to_workflow(workflow_dir, p))
    return list(dict.fromkeys(out))





def _collect_system_rtl_files(workflow_dir: str, state: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []

    # 1) Canonical source: top-assembly-generated sim filelist
    filelist_value = state.get("system_rtl_filelist_sim")
    if isinstance(filelist_value, list):
        candidates.extend(_normalize_path_list(filelist_value))
    elif isinstance(filelist_value, str) and filelist_value.strip():
        candidates.extend(_normalize_path_list(filelist_value))

    # 2) On-disk canonical filelist
    filelist_path = _find_system_rtl_filelist(workflow_dir)
    if filelist_path:
        candidates.extend(_normalize_path_list(filelist_path))

    if candidates:
        return list(dict.fromkeys(candidates))

    # 3) Legacy fallbacks only after canonical filelist sources
    for key in ("system_rtl_files", "rtl_inputs", "rtl_files"):
        candidates.extend(_normalize_path_list(state.get(key)))

    if candidates:
        return list(dict.fromkeys(candidates))

    return []


def _infer_top_module(state: Dict[str, Any], integration: Dict[str, Any], soc_top_sim_path: Optional[str], rtl_files: List[str]) -> str:
    for key in ("top_module", "soc_top_sim_module"):
        v = state.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    top = integration.get("top") or {}
    sim_module = top.get("sim_module")
    if isinstance(sim_module, str) and sim_module.strip():
        return sim_module.strip()

    if soc_top_sim_path and os.path.exists(soc_top_sim_path):
        mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
        try:
            with open(soc_top_sim_path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            pass

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
    return "soc_top_sim"


def _default_tests_from_testcases_json(path: Optional[str]) -> List[str]:
    data = _safe_read_json(path)
    tests = data.get("default_tests")
    if isinstance(tests, list) and tests:
        return [str(t) for t in tests if str(t).strip()]
    items = data.get("tests")
    out: List[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
    return list(dict.fromkeys(out))


def _gen_regression_runner(top: str, default_tests: List[str]) -> str:
    return f'''"""Regression runner for system-level Cocotb + Verilator simulation."""

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
        "stdout_tail": (p.stdout or "").splitlines()[-120:],
        "stderr_tail": (p.stderr or "").splitlines()[-120:],
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
        "type": "system_simulation_regression",
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
    workflow_dir: str,
    system_integration_intent_json: Optional[str],
    soc_top_sim_path: Optional[str],
    rtl_files: List[str],
    default_tests: List[str],
    tb_root: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    tb_makefile = state.get("tb_makefile") or os.path.join(tb_root, "Makefile")
    tb_test_py = state.get("tb_test_py") or os.path.join(tb_root, f"test_{top}.py")
    tb_testcases_json = state.get("tb_testcases_json") or os.path.join(tb_root, "testcases.json")
    tb_contract_json = state.get("tb_contract_json") or os.path.join(tb_root, "tb_contract.json")
    reports_dir = os.path.join(tb_root, "reports")

    return {
        "type": "vv_system_simulation_manifest",
        "version": "1.1",
        "top_module": top,
        "system_integration_intent_json": _rel_to_workflow(workflow_dir, system_integration_intent_json),
        "soc_top_sim_path": _rel_to_workflow(workflow_dir, soc_top_sim_path),
        # canonical resolved RTL set only; do not duplicate filelist path into manifest
        "rtl_files": _rel_list_to_workflow(workflow_dir, rtl_files),
        "default_tests": default_tests,
        "runner": _rel_to_workflow(workflow_dir, os.path.join(tb_root, "run_regression.py")),
        "makefile": _rel_to_workflow(workflow_dir, tb_makefile),
        "test_py": _rel_to_workflow(workflow_dir, tb_test_py),
        "testcases_json": _rel_to_workflow(workflow_dir, tb_testcases_json),
        "tb_contract_json": _rel_to_workflow(workflow_dir, tb_contract_json),
        "reports_dir": _rel_to_workflow(workflow_dir, reports_dir),
        "simulator": "verilator",
        "regmap_json": _rel_to_workflow(
            workflow_dir,
            state.get("digital_regmap_json") or state.get("regmap_json") or _find_regmap_json(workflow_dir),
        ),
        "sva_assertions_path": _rel_to_workflow(
            workflow_dir,
            state.get("system_sva_assertions_path") or state.get("sva_assertions_path"),
        ),
        "sva_bind_path": _rel_to_workflow(
            workflow_dir,
            state.get("system_sva_bind_path") or state.get("sva_bind_path"),
        ),
        "coverage_model_py": _rel_to_workflow(
            workflow_dir,
            state.get("system_coverage_model_py") or state.get("coverage_model_py"),
        ),
        "coverage_spec_json": _rel_to_workflow(
            workflow_dir,
            state.get("system_coverage_spec_json") or state.get("coverage_spec_json"),
        ),
        "functional_coverage_summary_json": _rel_to_workflow(
            workflow_dir,
            state.get("system_functional_coverage_summary_json") or state.get("functional_coverage_summary_json"),
        ),
        "functional_coverage_md": _rel_to_workflow(
            workflow_dir,
            state.get("system_functional_coverage_md") or state.get("functional_coverage_md"),
        ),
        "verification_sources_mk": _rel_to_workflow(
            workflow_dir,
            state.get("tb_verification_sources_mk"),
        ),
    }



def run_agent(state: dict) -> dict:
    agent_name = "System Simulation Control Agent"
    print("\n🎛️ Running System Simulation Control Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "system_simulation_control_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System Simulation Control Agent Log\n")

    integration_json_path = (
        state.get("system_integration_intent_json")
        or _find_system_integration_json(workflow_dir)
    )
    soc_top_sim_path = (
        state.get("system_top_sim_path")
        or state.get("soc_top_sim_path")
        or _find_soc_top_sim_path(workflow_dir)
    )
    integration = _safe_read_json(integration_json_path)
    rtl_files = _collect_system_rtl_files(workflow_dir, state)
    if not rtl_files:
        raise FileNotFoundError("No system RTL files found for simulation control")

    top = _infer_top_module(state, integration, soc_top_sim_path, rtl_files)

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    testcases_json_path = state.get("tb_testcases_json") or os.path.join(tb_root, "testcases.json")

    default_tests = _default_tests_from_testcases_json(testcases_json_path)
    if not default_tests:
        default_tests = state.get("vv_testcases") or ["system_smoke_test", "integrated_input_sanity"]
    if not default_tests:
        default_tests = ["system_smoke_test"]
    default_tests = [str(t) for t in default_tests if str(t).strip()]
    default_tests = list(dict.fromkeys(default_tests))

    _log(log_path, f"workflow_dir={workflow_dir}")
    _log(log_path, f"integration_json_path={integration_json_path}")
    _log(log_path, f"soc_top_sim_path={soc_top_sim_path}")
    _log(log_path, f"top_module={top}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log(log_path, f"default_tests={default_tests}")
    _log(log_path, f"rtl_files={json.dumps(rtl_files, indent=2)}")

    artifacts: Dict[str, Any] = {}

    runner_py = _gen_regression_runner(top, default_tests)
    runner_py_path = os.path.join(tb_root, "run_regression.py")
    _write_file(runner_py_path, runner_py)
    artifacts["runner_py"] = _record_text(workflow_id, agent_name, "vv/tb", "run_regression.py", runner_py)

    sim_manifest = _gen_simulation_manifest(
        top=top,
        workflow_dir=workflow_dir,
        system_integration_intent_json=integration_json_path,
        soc_top_sim_path=soc_top_sim_path,
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

    readme = f"""# System Simulation Control

Generated files:
- `run_regression.py`: orchestrates multi-seed runs via `make TESTCASE=...`
- `simulation_manifest.json`: consolidated simulation-control manifest for the integrated system DUT

Key resolved inputs:
- Top module: `{top}`
- System integration intent: `{integration_json_path or ''}`
- SoC top (sim): `{soc_top_sim_path or ''}`
- RTL file count: `{len(rtl_files)}`

Seed management:
- override with `RANDOM_SEED=<int>`

Usage:
```bash
python run_regression.py --tests {' '.join(default_tests)} --seeds 1 2 3 4 5
```
"""
    readme_path = os.path.join(tb_root, "SIM_CONTROL.md")
    _write_file(readme_path, readme)
    artifacts["sim_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "SIM_CONTROL.md", readme)

    tools_detected = {
        "verilator": bool(_which("verilator")),
        "make": bool(_which("make")),
        "python3": bool(_which("python3")),
    }

    report = {
        "type": "vv_system_simulation_control_generation",
        "version": "1.1",
        "top_module": top,
        "system_integration_intent_json": _rel_to_workflow(workflow_dir, integration_json_path),
        "soc_top_sim_path": _rel_to_workflow(workflow_dir, soc_top_sim_path),
        "rtl_file_count": len(rtl_files),
        "rtl_files": _rel_list_to_workflow(workflow_dir, rtl_files),
        "default_tests": default_tests,
        "simulation_manifest_json": _rel_to_workflow(workflow_dir, sim_manifest_path),
        "tb_makefile": _rel_to_workflow(workflow_dir, state.get("tb_makefile") or os.path.join(tb_root, "Makefile")),
        "tb_test_py": _rel_to_workflow(workflow_dir, state.get("tb_test_py") or os.path.join(tb_root, f"test_{top}.py")),
        "tb_testcases_json": _rel_to_workflow(workflow_dir, testcases_json_path),
        "tb_contract_json": _rel_to_workflow(workflow_dir, state.get("tb_contract_json") or os.path.join(tb_root, "tb_contract.json")),
        "tools_detected": tools_detected,
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    report_path = os.path.join(tb_root, "system_sim_control_generation_report.json")
    _write_file(report_path, rep_txt)
    artifacts["report"] = _record_text(
        workflow_id,
        agent_name,
        "vv/tb",
        "system_sim_control_generation_report.json",
        rep_txt,
    )
    artifacts["log"] = _record_text(
        workflow_id,
        agent_name,
        "vv",
        "system_simulation_control_agent.log",
        open(log_path, "r", encoding="utf-8").read(),
    )

    state.setdefault("vv", {})
    state["vv"]["sim_control"] = report
    state["system_simulation_manifest_json"] = sim_manifest_path
    state["simulation_manifest_json"] = sim_manifest_path
    state["system_simulation_runner_py"] = runner_py_path
    state["simulation_runner_py"] = runner_py_path
    state["vv_testcases"] = default_tests
    state["testcases"] = default_tests
    state["top_module"] = top
    state["system_sim_testcases"] = list(default_tests)
    state["simulation_testcases"] = list(default_tests)
    return state
