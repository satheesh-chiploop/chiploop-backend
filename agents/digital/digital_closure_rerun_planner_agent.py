import json
from pathlib import Path
from typing import Any, Dict

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Closure Rerun Planner Agent"


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    workflow_dir = Path(str(state.get("workflow_dir") or f"backend/workflows/{workflow_id}"))
    iteration = int(state.get("closure_iteration_index") or 1)
    out_dir = workflow_dir / "verify_closure" / f"iteration_{iteration:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    source_handoff = state.get("source_verification_source_handoff") if isinstance(state.get("source_verification_source_handoff"), dict) else {}
    source_arch2rtl = str(
        state.get("source_arch2rtl_workflow_id")
        or state.get("from_workflow_id")
        or source_handoff.get("source_workflow_id")
        or ""
    ).strip()
    if not source_arch2rtl:
        raise RuntimeError("Cannot plan closure rerun: source Arch2RTL workflow id was not found in prior Verify handoff.")

    tests = state.get("vv_testcases") if isinstance(state.get("vv_testcases"), list) else []
    seeds = state.get("simulation_seeds") if isinstance(state.get("simulation_seeds"), list) else []
    manifest = {
        "type": "closure_rerun_manifest",
        "iteration": iteration,
        "source_verify_workflow_id": state.get("source_verify_workflow_id"),
        "source_arch2rtl_workflow_id": source_arch2rtl,
        "rerun_mode": state.get("rerun_mode") or "coverage_targeted",
        "tests": tests,
        "seeds": seeds,
        "verification_plan_updated": bool(state.get("verification_plan")),
        "coverage_plan_updated": bool(state.get("coverage_plan")),
    }
    txt = json.dumps(manifest, indent=2)
    (out_dir / "rerun_manifest.json").write_text(txt, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "rerun_manifest.json", txt)

    state["rtl_source_mode"] = "from_arch2rtl"
    state["from_workflow_id"] = source_arch2rtl
    state["source_arch2rtl_workflow_id"] = source_arch2rtl
    state["upstream_workflows"] = {"arch2rtl": source_arch2rtl, "verify": state.get("source_verify_workflow_id")}
    state["random_vs_directed"] = "both"
    state["closure_rerun_manifest"] = manifest
    return state
