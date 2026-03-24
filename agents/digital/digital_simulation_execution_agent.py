import json
import logging
import os
import subprocess
from datetime import datetime

logger = logging.getLogger("chiploop")


def _log(msg):
    logger.info(msg)
    print(msg)


def run_agent(state: dict) -> dict:
    _log("=== Simulation Execution Agent ===")

    workflow_dir = state.get("workflow_dir")
    tb_root = os.path.join(workflow_dir, "vv", "tb")
    reports_dir = os.path.join(tb_root, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    manifest_path = state.get("simulation_manifest_json") or os.path.join(tb_root, "simulation_manifest.json")

    if not os.path.exists(manifest_path):
        raise Exception("simulation_manifest.json not found")

    manifest = json.load(open(manifest_path))

    tests = manifest.get("default_tests", [])
    seeds = state.get("simulation_seeds", [1])

    results = []

    for t in tests:
        for s in seeds:
            _log(f"Running {t} seed={s}")

            env = os.environ.copy()
            env["RANDOM_SEED"] = str(s)

            try:
                p = subprocess.run(
                    ["make", f"TESTCASE={t}"],
                    cwd=tb_root,
                    capture_output=True,
                    text=True,
                )

                results.append({
                    "testcase": t,
                    "seed": s,
                    "pass": p.returncode == 0,
                    "rc": p.returncode,
                })

            except Exception as e:
                results.append({
                    "testcase": t,
                    "seed": s,
                    "pass": False,
                    "error": str(e),
                })

    summary = {
        "total": len(results),
        "pass": sum(1 for r in results if r["pass"]),
        "fail": sum(1 for r in results if not r["pass"]),
        "results": results
    }

    out_path = os.path.join(reports_dir, "simulation_execution_summary.json")
    json.dump(summary, open(out_path, "w"), indent=2)

    _log(f"Execution summary written: {out_path}")

    state["simulation_execution_summary_json"] = out_path
    return state