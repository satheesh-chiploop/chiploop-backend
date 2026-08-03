from typing import Any, Dict


ROUTES = {
    "system_architecture": "/apps/system-architecture",
    "digital_design": "/apps/arch2rtl",
    "fpga_exploration": "/apps/fpga-target-explorer",
    "digital_implementation": "/apps/fpga-implementation",
    "firmware": "/apps/system-firmware",
    "product_map": "/apps/system-product-builder",
}


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    execution = state["physics_execution"]
    metrics = execution["simulation"]["metrics"]
    physics_passed = all(metrics["checks"].values()) and metrics["steady_state_speed_error_percent"] <= state["requirements_contract"]["accuracy"]["maximum_error_percent"]
    fixed_point_passed = bool(execution.get("fixed_point", {}).get("passed"))
    rtl_smoke_passed = bool(execution.get("rtl", {}).get("verification", {}).get("smoke_passed"))
    stages = [
        {"id": "requirements", "owner": "physical_ai", "status": "completed"},
        {"id": "model_selection", "owner": "physical_ai", "status": "completed"},
        {"id": "physics_validation", "owner": "physical_ai", "status": "completed" if physics_passed else "needs_revision"},
        {"id": "fixed_point_validation", "owner": "physical_ai", "status": "completed" if fixed_point_passed else "needs_revision"},
        {"id": "rtl_generation", "owner": "physical_ai", "status": "completed" if rtl_smoke_passed else "needs_verification"},
    ]
    for child, route in ROUTES.items():
        stages.append({"id": child, "owner": "existing_loop", "status": "ready" if physics_passed and fixed_point_passed and rtl_smoke_passed else "blocked", "app_path": route})
    handoff = {
        "schema": "chiploop.physical_ai.child_handoff.v1",
        "parent_workflow_id": state.get("workflow_id"),
        "source_model_id": state["selected_physics_model"]["model_id"],
        "source_contract": execution["files"]["design_contract"],
        "reference_vectors": execution["files"]["equation_timeseries"],
        "fixed_point_vectors": execution["files"]["fixed_point_vectors"],
        "rtl_numeric_contract": execution["files"]["rtl_numeric_contract"],
        "rtl_manifest": execution["files"]["motor_rtl_manifest"],
        "rtl_top": execution["files"]["rtl_motor_control_top"],
        "requirements": state["requirements_contract"],
        "return_to_parent": True,
    }
    return {**state, "physical_ai_loop": {"physics_passed": physics_passed, "fixed_point_passed": fixed_point_passed, "rtl_smoke_passed": rtl_smoke_passed, "stages": stages, "child_handoff": handoff}}
