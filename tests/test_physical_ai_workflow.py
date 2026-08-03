import json

import pytest

from physical_ai.model_registry import get_physics_model, list_physics_models
from physical_ai.workflow import run_physical_ai_workflow
from studio_contract.registry import load_registry


def test_pmsm_equation_model_is_ready_and_fpga_compatible():
    model = get_physics_model("chiploop.pmsm.dq.v1")
    assert model["availability"] == "ready"
    assert model["gpu_required"] is False
    assert "fpga" in model["implementation_targets"]
    assert any(item["availability"] == "requires_gpu_worker" for item in list_physics_models())


def test_generic_physical_ai_parent_workflow_executes_motor_adapter(tmp_path):
    result = run_physical_ai_workflow(
        {
            "application": "pmsm_motor_control",
            "objective": "Validate motor control",
            "physics_domain": "motor_control",
            "physics_model_id": "chiploop.pmsm.dq.v1",
            "implementation_target": "fpga",
            "maximum_error_percent": 3.0,
            "parameters": {"rated_speed_rpm": 3000, "load_torque_nm": 0.15},
        },
        str(tmp_path),
        workflow_id="parent-123",
    )
    assert result["status"] == "physics_validated"
    assert result["loop"]["child_handoff"]["parent_workflow_id"] == "parent-123"
    assert result["loop"]["stages"][3]["owner"] == "existing_loop"
    assert result["loop"]["stages"][3]["status"] == "ready"
    for path in result["files"].values():
        assert path


def test_unavailable_gpu_model_is_not_executed(tmp_path):
    with pytest.raises(ValueError, match="requires_gpu_worker"):
        run_physical_ai_workflow(
            {
                "physics_domain": "automotive_aerodynamics",
                "physics_model_id": "nvidia.domino.automotive_aero",
                "implementation_target": "gpu_service",
            },
            str(tmp_path),
        )


def test_physical_ai_agents_and_workflow_are_registered():
    registry = load_registry("registry")
    workflow = registry.workflows["Physical_AI_Loop"]
    assert workflow.loop_type == "physical_ai"
    assert workflow.agents == [
        "Physical AI Requirements Agent",
        "Physical AI Model Selection Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Orchestrator Agent",
    ]


def test_main_registers_generic_physical_ai_endpoints():
    main = open("main.py", encoding="utf-8").read()
    assert '@app.get("/apps/physical-ai/models")' in main
    assert '@app.post("/apps/physical-ai/run")' in main
