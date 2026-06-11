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
    source_manifest = state.get("source_simulation_manifest") if isinstance(state.get("source_simulation_manifest"), dict) else {}
    source_manifest_type = str(source_manifest.get("type") or "").strip().lower()
    explicit_system_sim = bool(
        state.get("source_system_sim_workflow_id")
        or str(state.get("template_workflow_name") or "").startswith("System_")
        or source_manifest_type.startswith("system_")
    )
    source_system_sim = str(
        state.get("source_system_sim_workflow_id")
        or (state.get("source_verify_workflow_id") if explicit_system_sim else "")
        or (state.get("parent_workflow_id") if explicit_system_sim else "")
        or ""
    ).strip()
    is_system_sim = bool(source_system_sim and explicit_system_sim)
    source_arch2rtl = str(
        state.get("source_arch2rtl_workflow_id")
        or state.get("from_workflow_id")
        or source_handoff.get("source_workflow_id")
        or ""
    ).strip()
    if not source_arch2rtl and not is_system_sim:
        raise RuntimeError("Cannot plan closure rerun: source Arch2RTL workflow id was not found in prior Verify handoff.")

    tests = state.get("vv_testcases") if isinstance(state.get("vv_testcases"), list) else []
    seeds = state.get("simulation_seeds") if isinstance(state.get("simulation_seeds"), list) else []
    random_vs_directed = str(state.get("random_vs_directed") or "both").strip().lower()
    if random_vs_directed not in {"directed", "random", "both"}:
        random_vs_directed = "both"
    manifest = {
        "type": "closure_rerun_manifest",
        "iteration": iteration,
        "source_verify_workflow_id": state.get("source_verify_workflow_id"),
        "source_arch2rtl_workflow_id": source_arch2rtl or None,
        "source_system_sim_workflow_id": source_system_sim or None,
        "closure_context": "system_sim" if is_system_sim else "digital_verify",
        "rerun_mode": state.get("rerun_mode") or "coverage_targeted",
        "random_vs_directed": random_vs_directed,
        "tests": tests,
        "seeds": seeds,
        "verification_plan_updated": bool(state.get("verification_plan")),
        "coverage_plan_updated": bool(state.get("coverage_plan")),
    }
    txt = json.dumps(manifest, indent=2)
    (out_dir / "rerun_manifest.json").write_text(txt, encoding="utf-8")
    save_text_artifact_and_record(workflow_id, AGENT_NAME, f"verify_closure/iteration_{iteration:03d}", "rerun_manifest.json", txt)

    if is_system_sim:
        state["rtl_source_mode"] = "from_system_rtl"
        state["source_system_sim_workflow_id"] = source_system_sim
        state["upstream_workflows"] = {"system_sim": source_system_sim}
    else:
        state["rtl_source_mode"] = "from_arch2rtl"
        state["from_workflow_id"] = source_arch2rtl
        state["source_arch2rtl_workflow_id"] = source_arch2rtl
        state["upstream_workflows"] = {"arch2rtl": source_arch2rtl, "verify": state.get("source_verify_workflow_id")}
    state["random_vs_directed"] = random_vs_directed
    state["closure_rerun_manifest"] = manifest
    return state
