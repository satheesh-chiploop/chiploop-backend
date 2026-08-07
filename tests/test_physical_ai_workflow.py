import json

import pytest

import agents.physical_ai.physical_ai_architecture_agent as architecture_agent
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
    progress = []
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
        progress=lambda agent, status, phase: progress.append((agent, status, phase)),
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
    assert [event[1] for event in progress] == ["started", "completed"] * 5
    assert [event[0] for event in progress if event[1] == "completed"] == [
        "Physical AI Requirements Agent",
        "Physical AI Model Selection Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Architecture Agent",
        "Physical AI Orchestrator Agent",
    ]


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


def test_pretrained_gpu_surrogate_supports_cpu_architecture_journey(tmp_path):
    result = run_physical_ai_workflow(
        {
            "application": "automotive_aerodynamics_architecture",
            "objective": "Define active-aero digital IP",
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "implementation_target": "asic",
            "execution_mode": "architecture",
            "implementation_path": "fpga_then_asic",
            "parameters": {"stream_velocity_mps": 38.89},
            "hem_enabled": False,
        },
        str(tmp_path),
        workflow_id="aero-parent-1",
    )
    assert result["status"] == "ready_for_digital_design"
    assert result["physics_execution"]["inference_status"] == "not_executed"
    assert result["loop"]["architecture_passed"] is True
    assert result["loop"]["physics_passed"] is False
    assert result["loop"]["child_handoff"]["next_loop"] == "digital_design"
    assert result["loop"]["child_handoff"]["implementation_path"] == "fpga_then_asic"
    assert next(stage for stage in result["loop"]["stages"] if stage["id"] == "fpga_exploration")["status"] == "planned"
    assert next(stage for stage in result["loop"]["stages"] if stage["id"] == "digital_implementation")["status"] == "planned"
    assert result["physics_execution"]["digital_ip_spec"]["surrogate_is_not_rtl"] is True
    assert result["physics_execution"]["digital_ip_spec"]["top_module"] == "adaptive_aero_control_top"
    assert result["physics_execution"]["digital_ip_spec"]["project_name"] == "adaptive_aero_control"
    assert result["files"]["digital_ip_spec"]
    summary = json.loads(open(result["files"]["workflow_summary"], encoding="utf-8").read())
    assert summary["physics_execution"]["interface"]["inference"]["status"] == "not_executed"


def test_architecture_only_stops_before_rtl(tmp_path):
    result = run_physical_ai_workflow(
        {
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "implementation_target": "asic",
            "execution_mode": "architecture",
            "implementation_path": "architecture_only",
        },
        str(tmp_path),
    )
    assert result["status"] == "architecture_complete"
    assert result["loop"]["child_handoff"]["next_loop"] is None
    assert next(stage for stage in result["loop"]["stages"] if stage["id"] == "digital_design")["status"] == "not_requested"


def test_selected_agent_model_generates_rtl_ready_architecture(tmp_path, monkeypatch):
    response = {
        "product_name": "Active Aero Controller",
        "product_summary": "Controls active aerodynamic surfaces safely.",
        "architecture_decisions": ["Keep surrogate inference outside RTL"],
        "blocks": ["sensor_filter", "command_limiter", "safety_fsm"],
        "interfaces": ["AXI4-Lite", "sensor stream", "actuator command"],
        "safety_requirements": ["Fallback on stale command"],
        "rtl_spec_text": "Create a synthesizable active_aero_control_top with AXI4-Lite registers, sensor filtering, command limits, watchdog, and safe fallback.",
        "verification_goals": ["Verify stale-command fallback", "Verify command clamping"],
    }
    calls = []
    monkeypatch.setattr(architecture_agent, "complete_text", lambda prompt, **kwargs: calls.append((prompt, kwargs)) or json.dumps(response))
    result = run_physical_ai_workflow(
        {
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "implementation_target": "asic",
            "execution_mode": "architecture",
            "implementation_path": "digital_ip_asic",
            "generate_architecture_with_model": True,
            "model_policy": {"mode": "standard", "selected_model": "nvidia_nemotron"},
        },
        str(tmp_path),
    )
    assert calls
    assert calls[0][1]["agent_name"] == "Physical AI Architecture Agent"
    assert result["physics_execution"]["architecture"]["rtl_spec_text"].startswith("Create a synthesizable")
    assert result["physics_execution"]["architecture"]["top_module"] == "adaptive_aero_control_top"
    assert result["loop"]["child_handoff"]["rtl_spec_text"] == response["rtl_spec_text"]


def test_physical_ai_agents_and_workflow_are_registered():
    registry = load_registry("registry")
    workflow = registry.workflows["Physical_AI_Loop"]
    assert workflow.loop_type == "physical_ai"
    assert workflow.agents == [
        "Physical AI Requirements Agent",
        "Physical AI Model Selection Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Architecture Agent",
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
        "Physical AI Architecture Agent",
        "Physical AI Orchestrator Agent",
    ):
        assert agent_name in migration

    identity_migration = open(
        "supabase/migrations/phase_20260807_physical_ai_design_identity.sql",
        encoding="utf-8",
    ).read()
    assert "'adaptive_aero_control_top'" in identity_migration
    assert "'motor_control_top'" in identity_migration

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
