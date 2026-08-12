import json
from pathlib import Path

import pytest

import agents.physical_ai.physical_ai_architecture_agent as architecture_agent
import agents.physical_ai.physical_ai_application_intelligence_agent as application_intelligence_agent
import agents.physical_ai.physical_ai_partitioning_agent as partitioning_agent
from physical_ai.model_registry import get_physics_model, list_physics_models
from physical_ai.workflow import run_physical_ai_workflow
from studio_contract.registry import load_registry


def test_physical_ai_hem_default_explores_cross_vendor_fpga_targets():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'from agents.fpga.fpga_common import BOARD_REGISTRY' in main_source
    assert 'profile.get("support_tier") or "production"' in main_source
    assert 'profile.get("nextpnr_tool")' in main_source
    assert 'profile.get("device")' in main_source
    assert 'payload.get("candidate_boards") or _hem_physical_ai_fpga_candidates()' in main_source


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
    assert [event[1] for event in progress] == ["started", "completed"] * 8
    assert [event[0] for event in progress if event[1] == "completed"] == [
        "Physical AI Requirements Agent",
        "Application Intelligence Agent",
        "Physical AI Model Selection Agent",
        "Surrogate Discovery and Mapping Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Architecture Agent",
        "Hardware Software Partitioning Agent",
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


def test_active_aero_cpu_reference_builds_application_partition_and_policy(tmp_path):
    result = run_physical_ai_workflow(
        {
            "application": "intelligent_active_aerodynamics_controller",
            "objective": "Partition and prototype active aero control",
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "implementation_target": "fpga",
            "execution_mode": "cpu_reference",
            "implementation_path": "fpga_prototype",
            "operating_envelope": {"stream_velocity_mps": [20, 55]},
            "hem_enabled": False,
        },
        str(tmp_path),
        workflow_id="active-aero-reference",
    )

    assert result["status"] == "ready_for_digital_design"
    assert result["physics_execution"]["execution_mode"] == "cpu_reference"
    assert result["physics_execution"]["inference_status"] == "not_executed"
    assert result["application_intelligence"]["source_of_truth"] == "supabase"
    assert result["model_qualification"]["qualification_status"] == "provisionally_compatible"
    assert result["partition"]["decision"] == "heterogeneous"
    assert result["partition"]["schema"] == "chiploop.application_intelligence.partition.v2"
    assert result["partition"]["partition_phases"]["functional"] == {"status": "completed", "target_independent": True}
    refinement = result["partition"]["partition_phases"]["target_refinement"]
    assert refinement["status"] == "pending_board_selection"
    assert refinement["selected_mode"] is None
    assert refinement["firmware_gate"]["ready"] is False
    assert "fpga_onboard_cpu" in refinement["candidate_modes"]
    assert "fpga_external_host" in refinement["candidate_modes"]
    assert len(result["partition"]["jobs"]) == 5
    assert result["files"]["cpu_reference_results"]
    assert result["files"]["control_policy"]
    assert result["physics_execution"]["control_policy"]["format"] == "piecewise_linear_lut"
    assert len(result["physics_execution"]["cpu_reference"]["operating_points"]) == 5


def test_asic_partition_records_explicit_soc_architecture(tmp_path):
    result = run_physical_ai_workflow(
        {
            "application": "intelligent_active_aerodynamics_controller",
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "execution_mode": "architecture",
            "implementation_path": "digital_ip_asic",
            "deployment_architecture": "asic_soc",
            "hem_enabled": False,
        },
        str(tmp_path),
    )

    refinement = result["partition"]["partition_phases"]["target_refinement"]
    assert refinement["status"] == "selected"
    assert refinement["selected_mode"] == "asic_soc"
    assert "asic_digital_ip" in refinement["candidate_modes"]


def test_application_intelligence_and_partition_use_selected_agent_model(tmp_path, monkeypatch):
    application_response = {
        "name": "adaptive_aero_product",
        "objective": "Reduce drag safely",
        "physics_domain": "automotive_aerodynamics",
        "operating_envelope": {"stream_velocity_mps": [20, 55]},
        "constraints": {"safety": ["bounded command"]},
        "required_capabilities": ["estimate drag", "command actuator"],
        "acceptance_gates": ["command latency below 10 ms"],
        "workloads": [{"id": "drag_estimation", "inputs": ["vehicle_state"], "outputs": ["drag_estimate"]}],
        "interfaces": [{"id": "vehicle_state"}],
    }
    partition_response = {
        "decision": "heterogeneous",
        "jobs": [{
            "id": "control_guard",
            "target": "fpga",
            "status": "ready_for_rtl",
            "responsibility": "bound actuator commands",
            "inputs": ["requested_command"],
            "outputs": ["safe_command"],
            "latency_budget": "100 us",
            "rationale": "deterministic safety path",
        }],
        "interfaces": [{"from": "cpu_software", "to": "fpga", "contract": "versioned command"}],
        "decision_factors": ["latency", "safety"],
        "tradeoffs": ["surrogate inference remains outside RTL"],
    }
    architecture_response = {
        "product_name": "Active Aero Controller",
        "top_module": "adaptive_aero_control_top",
        "product_summary": "Safe active aero control",
        "architecture_decisions": ["Keep surrogate outside RTL"],
        "blocks": ["command_guard"],
        "interfaces": ["request response stream"],
        "safety_requirements": ["bounded command"],
        "rtl_spec_text": "Build a synthesizable bounded command controller.",
        "verification_goals": ["verify command bounds"],
    }
    calls = []
    monkeypatch.setattr(application_intelligence_agent, "complete_text", lambda prompt, **kwargs: calls.append(kwargs) or json.dumps(application_response))
    monkeypatch.setattr(architecture_agent, "complete_text", lambda prompt, **kwargs: calls.append(kwargs) or json.dumps(architecture_response))
    monkeypatch.setattr(partitioning_agent, "complete_text", lambda prompt, **kwargs: calls.append(kwargs) or json.dumps(partition_response))

    result = run_physical_ai_workflow(
        {
            "application": "intelligent_active_aerodynamics_controller",
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "execution_mode": "cpu_reference",
            "implementation_path": "fpga_prototype",
            "generate_architecture_with_model": True,
            "model_policy": {"mode": "standard", "selected_model": "nvidia_nemotron"},
        },
        str(tmp_path),
    )

    assert [call["agent_name"] for call in calls] == [
        "Application Intelligence Agent",
        "Physical AI Architecture Agent",
        "Hardware Software Partitioning Agent",
    ]
    assert result["application_intelligence"]["generation_status"] == "model_generated"
    assert result["partition"]["generation_status"] == "model_generated"
    assert result["partition"]["jobs"][0]["target"] == "fpga"


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
        "Application Intelligence Agent",
        "Physical AI Model Selection Agent",
        "Surrogate Discovery and Mapping Agent",
        "Physical AI Physics Execution Agent",
        "Physical AI Architecture Agent",
        "Hardware Software Partitioning Agent",
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
    assert '"App: Application Intelligence - Active Aero"' in main
    assert '"FPGA_Target_Explorer"' in main
    assert '"FPGA_RTL_to_Bitstream"' in main
    assert 'plan.append("firmware_product")' in main
    assert '"system_rtl_workflow_id": source_arch2rtl' in main
    assert '"system_software": True' in main
    assert '"system_validation": True' in main
    assert '"system_product": True' in main
    assert '"partition_plan": result.get("partition") or {}' in main
    assert '"software_goal": f"Build the host control' in main
    assert '"Device Layer / Firmware"' in main
    assert 'device_layer_role = "host_device_layer" if external_host else "embedded_firmware"' in main
    assert '"x86_64-unknown-linux-gnu" if external_host else "riscv32imac-unknown-none-elf"' in main
    assert 'automation_payload["fpga_source_workflow_id"] = child_workflow_id' in main
    assert 'payload.get("fpga_source_workflow_id") or source_arch2rtl' in main
    assert '"_fail_fast_on_agent_error": True' in main
    assert 'dashboard_stage=next_meta.get("stage") or next_template.lower()' in main
    assert 'nested_status = _hem_run_status' in main
    assert 'nested_status != "completed"' in main
    assert 'Preserve that deeper Supabase state' in main


def test_physical_ai_reuses_existing_firmware_and_software_collateral_contracts():
    main = open("main.py", encoding="utf-8").read()
    firmware_ingest = open("agents/embedded/embedded_digital_handoff_ingest_agent.py", encoding="utf-8").read()
    firmware_package = open("agents/system/system_software_handoff_package_agent.py", encoding="utf-8").read()
    software_ingest = open("agents/system/system_software_handoff_ingest_agent.py", encoding="utf-8").read()

    assert '"system_rtl_workflow_id": source_arch2rtl' in main
    assert '"from_workflow_id": source_arch2rtl' in main
    assert 'digital/digital_regmap.json' in firmware_ingest
    assert 'system/package/system_rtl_package.json' in firmware_ingest
    assert 'REQUIRED_FIRMWARE_MANIFEST = "firmware/firmware_manifest.json"' in firmware_package
    assert '"system/software_handoff/system_software_handoff.json"' in software_ingest


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

    runs_migration = open(
        "supabase/migrations/phase_20260807_physical_ai_runs_loop_type.sql",
        encoding="utf-8",
    ).read()
    assert "alter table if exists public.runs" in runs_migration
    assert "runs_loop_type_chk" in runs_migration
    assert "'physical_ai'" in runs_migration

    apps_page = open("../frontend/app/apps/page.tsx", encoding="utf-8").read()
    application_app = open("../frontend/app/apps/application-intelligence/page.tsx", encoding="utf-8").read()
    loops_page = open("../frontend/app/loops/page.tsx", encoding="utf-8").read()
    studio_page = open("../frontend/app/workflow/page.tsx", encoding="utf-8").read()
    assert 'title: "Physical AI Design Studio"' in apps_page
    assert 'key: "physical-ai-pmsm"' in apps_page
    assert 'router.push("/apps/application-intelligence")' in apps_page
    assert 'export { default } from "../physical-ai/page"' in application_app
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
