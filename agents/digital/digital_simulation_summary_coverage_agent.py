import json
import logging
import os

logger = logging.getLogger("chiploop")


def _log(msg):
    logger.info(msg)
    print(msg)


def run_agent(state: dict) -> dict:
    _log("=== Simulation Summary Coverage Agent ===")

    workflow_dir = state.get("workflow_dir")
    reports_dir = os.path.join(workflow_dir, "vv", "tb", "reports")

    sim_path = state.get("simulation_execution_summary_json")
    cov_path = os.path.join(reports_dir, "functional_coverage_summary.json")

    sim = json.load(open(sim_path)) if os.path.exists(sim_path) else {}
    cov = json.load(open(cov_path)) if os.path.exists(cov_path) else {}

    summary = {
        "simulation": {
            "total": sim.get("total"),
            "pass": sim.get("pass"),
            "fail": sim.get("fail"),
        },
        "coverage": {
            "functional_coverage_pct": cov.get("functional_coverage_pct"),
        }
    }

    out_path = os.path.join(reports_dir, "final_summary.json")
    json.dump(summary, open(out_path, "w"), indent=2)

    _log(f"Final summary written: {out_path}")

    state["final_summary"] = out_path
    return state