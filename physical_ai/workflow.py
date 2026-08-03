import json
from pathlib import Path
from typing import Any, Dict

from agents.physical_ai import (
    run_model_selection_agent,
    run_orchestrator_agent,
    run_physics_execution_agent,
    run_requirements_agent,
)


def _write_json(root: Path, name: str, value: Dict[str, Any]) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def run_physical_ai_workflow(payload: Dict[str, Any], artifact_dir: str, *, workflow_id: str = "") -> Dict[str, Any]:
    root = Path(artifact_dir) / "physical_ai"
    state: Dict[str, Any] = {
        **payload,
        "artifact_dir": str(root),
        "workflow_id": workflow_id,
        "model_policy": payload.get("model_policy") or {"mode": "standard", "selected_model": "chiploop_default"},
    }
    state = run_requirements_agent(state)
    state = run_model_selection_agent(state)
    state = run_physics_execution_agent(state)
    state = run_orchestrator_agent(state)

    files = dict(state["physics_execution"]["files"])
    files.update({
        "requirements_contract": _write_json(root, "requirements_contract.json", state["requirements_contract"]),
        "selected_physics_model": _write_json(root, "selected_physics_model.json", state["selected_physics_model"]),
        "child_handoff": _write_json(root, "child_handoff.json", state["physical_ai_loop"]["child_handoff"]),
        "loop_state": _write_json(root, "physical_ai_loop_state.json", state["physical_ai_loop"]),
    })
    result = {
        "schema": "chiploop.physical_ai.workflow_result.v1",
        "status": "ready_for_fpga_exploration" if state["physical_ai_loop"]["physics_passed"] and state["physical_ai_loop"]["fixed_point_passed"] and state["physical_ai_loop"]["rtl_smoke_passed"] else "needs_revision",
        "requirements": state["requirements_contract"],
        "physics_model": state["selected_physics_model"],
        "physics_execution": {
            "status": state["physics_execution"]["status"],
            "metrics": state["physics_execution"]["simulation"]["metrics"],
            "operating_sweep": state["physics_execution"]["operating_sweep"],
            "fixed_point": state["physics_execution"]["fixed_point"],
            "rtl_numeric_contract": state["physics_execution"]["rtl_numeric_contract"],
            "rtl": state["physics_execution"]["rtl"],
        },
        "loop": state["physical_ai_loop"],
        "hem": {
            "enabled": bool(payload.get("hem_enabled", True)),
            "mode": str(payload.get("hem_mode") or "fixed"),
            "goal": str(payload.get("hem_goal") or "product_demo"),
            "stage_toggles": payload.get("hem_stage_toggles") or {"fpga_exploration": True, "fpga_bitstream": True, "firmware_product": True},
            "start_condition": "physics_passed AND fixed_point_passed AND rtl_smoke_passed",
        },
        "files": files,
    }
    summary_path = _write_json(root, "physical_ai_workflow_summary.json", result)
    result["files"]["workflow_summary"] = summary_path
    return result
