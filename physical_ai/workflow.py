import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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


def run_physical_ai_workflow(
    payload: Dict[str, Any],
    artifact_dir: str,
    *,
    workflow_id: str = "",
    progress: Optional[Callable[[str, str, str], None]] = None,
) -> Dict[str, Any]:
    root = Path(artifact_dir) / "physical_ai"
    state: Dict[str, Any] = {
        **payload,
        "artifact_dir": str(root),
        "workflow_id": workflow_id,
        "model_policy": payload.get("model_policy") or {"mode": "standard", "selected_model": "chiploop_default"},
    }
    steps = (
        ("Physical AI Requirements Agent", "requirements_agent", run_requirements_agent),
        ("Physical AI Model Selection Agent", "model_selection_agent", run_model_selection_agent),
        ("Physical AI Physics Execution Agent", "physics_execution_agent", run_physics_execution_agent),
        ("Physical AI Orchestrator Agent", "orchestrator_agent", run_orchestrator_agent),
    )
    for agent_name, phase, execute in steps:
        if progress:
            progress(agent_name, "started", phase)
        state = execute(state)
        if progress:
            progress(agent_name, "completed", phase)

    files = dict(state["physics_execution"]["files"])
    files.update({
        "requirements_contract": _write_json(root, "requirements_contract.json", state["requirements_contract"]),
        "selected_physics_model": _write_json(root, "selected_physics_model.json", state["selected_physics_model"]),
        "child_handoff": _write_json(root, "child_handoff.json", state["physical_ai_loop"]["child_handoff"]),
        "loop_state": _write_json(root, "physical_ai_loop_state.json", state["physical_ai_loop"]),
    })
    architecture_mode = state["physics_execution"].get("execution_mode") == "architecture"
    if architecture_mode:
        execution_result = {
            "status": state["physics_execution"]["status"],
            "execution_mode": "architecture",
            "inference_status": state["physics_execution"]["inference_status"],
            "interface": state["physics_execution"]["interface"],
            "architecture": state["physics_execution"]["architecture"],
            "digital_ip_spec": state["physics_execution"]["digital_ip_spec"],
            "validation": state["physics_execution"]["validation"],
            "product_map": state["physics_execution"]["product_map"],
            "implementation_path": state["physics_execution"]["implementation_path"],
        }
    else:
        execution_result = {
            "status": state["physics_execution"]["status"],
            "execution_mode": "validated",
            "metrics": state["physics_execution"]["simulation"]["metrics"],
            "operating_sweep": state["physics_execution"]["operating_sweep"],
            "fixed_point": state["physics_execution"]["fixed_point"],
            "rtl_numeric_contract": state["physics_execution"]["rtl_numeric_contract"],
            "rtl": state["physics_execution"]["rtl"],
        }
    result = {
        "schema": "chiploop.physical_ai.workflow_result.v1",
        "status": ("architecture_complete" if state["physics_execution"].get("implementation_path") == "architecture_only" else "ready_for_digital_design") if architecture_mode else ("ready_for_fpga_exploration" if state["physical_ai_loop"]["physics_passed"] and state["physical_ai_loop"]["fixed_point_passed"] and state["physical_ai_loop"]["rtl_smoke_passed"] else "needs_revision"),
        "requirements": state["requirements_contract"],
        "physics_model": state["selected_physics_model"],
        "physics_execution": execution_result,
        "loop": state["physical_ai_loop"],
        "hem": {
            "enabled": bool(payload.get("hem_enabled", True)),
            "mode": str(payload.get("hem_mode") or "fixed"),
            "goal": str(payload.get("hem_goal") or "product_demo"),
            "stage_toggles": payload.get("hem_stage_toggles") or {"fpga_exploration": True, "fpga_bitstream": True, "firmware_product": True},
            "start_condition": "architecture_passed" if architecture_mode else "physics_passed AND fixed_point_passed AND rtl_smoke_passed",
        },
        "files": files,
    }
    summary_path = _write_json(root, "physical_ai_workflow_summary.json", result)
    result["files"]["workflow_summary"] = summary_path
    return result
