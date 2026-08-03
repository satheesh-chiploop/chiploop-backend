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
    assert result["status"] == "ready_for_fpga_exploration"
    assert result["physics_execution"]["fixed_point"]["passed"] is True
    assert result["hem"]["enabled"] is True
    assert result["hem"]["stage_toggles"] == {"fpga_exploration": True, "fpga_bitstream": True, "firmware_product": True}
    assert result["loop"]["child_handoff"]["parent_workflow_id"] == "parent-123"
    assert result["loop"]["stages"][3]["id"] == "fixed_point_validation"
    assert result["loop"]["stages"][3]["status"] == "completed"
    assert result["loop"]["stages"][4]["id"] == "rtl_generation"
    assert result["loop"]["stages"][4]["status"] == "completed"
    assert result["loop"]["stages"][5]["owner"] == "existing_loop"
    assert result["loop"]["stages"][5]["status"] == "ready"
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
    assert '@app.get("/apps/physical-ai/{workflow_id}/result")' in main
    assert '@app.post("/apps/physical-ai/run")' in main
    assert 'dashboard_path": f"/apps/physical-ai/results/{workflow_id}"' in main
    assert 'hem_enabled: bool = True' in main
    assert 'def _hem_continue_physical_ai_after_success' in main
    assert '"FPGA_Target_Explorer"' in main
    assert '"FPGA_RTL_to_Bitstream"' in main


def test_physical_ai_has_supabase_source_of_truth_migration():
    migration = open(
        "supabase/migrations/phase_20260803_physical_ai_source_of_truth.sql",
        encoding="utf-8",
    ).read()
    assert "create table if not exists public.physical_ai_models" in migration
    assert "'Physical_AI_Loop'" in migration
    assert "source_of_truth', 'supabase'" in migration
    assert "enable row level security" in migration
    assert "drop constraint if exists workflows_loop_type_chk" in migration
    assert "'physical_ai'" in migration
    for agent_name in (
        "Physical AI Requirements Agent",
        "Physical AI Model Selection Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Orchestrator Agent",
    ):
        assert agent_name in migration

    apps_page = open("../frontend/app/apps/page.tsx", encoding="utf-8").read()
    loops_page = open("../frontend/app/loops/page.tsx", encoding="utf-8").read()
    studio_page = open("../frontend/app/workflow/page.tsx", encoding="utf-8").read()
    assert 'title: "Physical AI Design Studio"' in apps_page
    assert 'key: "physical-ai-pmsm"' in apps_page
    assert 'href: "/loops/physical-ai"' in loops_page
    assert '<option value="physical_ai">Physical AI Loop — Coming soon</option>' in studio_page
    assert 'Physical_AI_Loop: linearWorkflowDefinition' in studio_page


def test_model_selection_accepts_supabase_snapshot(tmp_path):
    model = get_physics_model("chiploop.pmsm.dq.v1")
    model["name"] = "Supabase governed PMSM"
    result = run_physical_ai_workflow(
        {
            "physics_domain": "motor_control",
            "physics_model_id": "chiploop.pmsm.dq.v1",
            "physics_model_record": model,
            "implementation_target": "fpga",
        },
        str(tmp_path),
    )
    assert result["physics_model"]["name"] == "Supabase governed PMSM"
