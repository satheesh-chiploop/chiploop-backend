import json
from pathlib import Path

import pytest

import agents.physical_ai.physical_ai_architecture_agent as architecture_agent
import agents.physical_ai.physical_ai_application_intelligence_agent as application_intelligence_agent
import agents.physical_ai.physical_ai_partitioning_agent as partitioning_agent
from physical_ai.model_registry import get_physics_model, list_physics_models
from physical_ai.soft_cpu import resolve_soft_cpu_config
from physical_ai.asic_cpu import resolve_asic_cpu_config
from physical_ai.workflow import run_physical_ai_workflow
from studio_contract.registry import load_registry

TEST_PROCESSOR_POLICY = {
    "schema": "chiploop.application_intelligence.processor_ip_policy.v2",
    "automatic_fpga_deployment": "fpga_external_host",
    "automatic_asic_deployment": "asic_digital_ip",
    "fpga_hard_cpu": {"availability": "board_contract_required"},
    "fpga_soft_cpu": {"availability": "preview", "default_core": "picorv32", "defaults": {"isa": "automatic", "bus": "automatic", "clock_mhz": 50, "instruction_memory_kib": 32, "data_memory_kib": 16, "interrupts": True, "uart": True, "debug": False}, "allowed_buses": ["wishbone", "axi4_lite", "native"], "cores": {"serv": {"label": "SERV", "license": "ISC", "profile": "minimum_area", "default_isa": "rv32i", "supported_isas": ["rv32i", "rv32im"], "default_bus": "wishbone", "estimated_logic_cells": 900, "estimated_bram_blocks": 8}, "picorv32": {"label": "PicoRV32", "license": "ISC", "profile": "balanced", "default_isa": "rv32imc", "supported_isas": ["rv32i", "rv32im", "rv32imc"], "default_bus": "wishbone", "estimated_logic_cells": 3000, "estimated_bram_blocks": 12}}, "integration_gate": {"cpu_rtl_required": True, "complete_system_synthesis_required": True, "default_status": "pending_cpu_rtl"}},
    "asic_soc_cpu": {"availability": "preview", "default_core": "picorv32", "defaults": {"isa": "automatic", "bus": "automatic", "clock_mhz": 100, "boot_rom_kib": 16, "sram_kib": 64, "interrupts": True, "debug": False, "clock_gating": True, "dft_scan_required": True}, "allowed_buses": ["apb", "axi4_lite", "wishbone", "native"], "cores": {"picorv32": {"label": "PicoRV32", "license": "ISC", "profile": "balanced", "default_isa": "rv32imc", "supported_isas": ["rv32i", "rv32im", "rv32imc"], "default_bus": "apb"}, "vexriscv": {"label": "VexRiscv", "license": "MIT", "profile": "performance", "default_isa": "rv32imc", "supported_isas": ["rv32imc"], "default_bus": "axi4_lite"}}, "integration_gate": {"cpu_rtl_required": True, "memory_macro_mapping_required": True, "complete_soc_synthesis_required": True, "complete_system_synthesis_required": True, "default_status": "pending_cpu_rtl"}},
}


def _mock_upstream_model_planners(monkeypatch):
    monkeypatch.setattr(
        application_intelligence_agent,
        "complete_text",
        lambda *_args, **_kwargs: json.dumps({
            "name": "Test application",
            "objective": "Test objective",
            "physics_domain": "automotive_aerodynamics",
            "operating_envelope": {},
            "constraints": {},
            "required_capabilities": ["bounded control"],
            "acceptance_gates": ["outputs are bounded"],
            "workloads": ["control"],
            "interfaces": ["control/status"],
        }),
    )
    monkeypatch.setattr(
        partitioning_agent,
        "complete_text",
        lambda *_args, **_kwargs: json.dumps({
            "decision": "heterogeneous",
            "jobs": [{
                "id": "control",
                "target": "fpga_or_asic",
                "status": "ready_for_rtl",
                "responsibility": "bounded control",
                "inputs": [],
                "outputs": [],
                "latency_budget": "bounded",
                "rationale": "deterministic execution",
            }],
            "interfaces": [],
            "decision_factors": ["determinism"],
            "tradeoffs": [],
        }),
    )


def test_partitioning_agent_repairs_json_syntax_without_fallback(tmp_path, monkeypatch):
    repaired = {
        "decision": "heterogeneous",
        "jobs": [{
            "id": "control",
            "target": "fpga_or_asic",
            "status": "ready_for_rtl",
            "responsibility": "bounded control",
            "inputs": [],
            "outputs": [],
            "latency_budget": "bounded",
            "rationale": "deterministic execution",
        }],
        "interfaces": [],
        "decision_factors": ["determinism"],
        "tradeoffs": [],
    }
    outputs = iter(['{"decision":"heterogeneous" "jobs":[]}', json.dumps(repaired)])
    monkeypatch.setattr(partitioning_agent, "complete_text", lambda *_args, **_kwargs: next(outputs))
    state = {
        "artifact_dir": str(tmp_path),
        "generate_architecture_with_model": True,
        "requirements_contract": {
            "application": "test",
            "implementation_path": "fpga_prototype",
            "deployment_architecture": "fpga_soft_cpu",
        },
        "selected_physics_model": {"model_id": "test-model"},
        "physics_execution": {"inference_status": "not_executed", "architecture": {}},
        "application_contract": {},
    }

    result = partitioning_agent.run_agent(state)

    assert result["partition_plan"]["generation_status"] == "model_generated"
    assert Path(result["partition_plan"]["generation_artifacts"]["raw"]).exists()
    assert Path(result["partition_plan"]["generation_artifacts"]["syntax_repaired_raw"]).exists()


def test_physical_ai_hem_default_explores_cross_vendor_fpga_targets():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'from agents.fpga.fpga_common import BOARD_REGISTRY' in main_source
    assert 'profile.get("support_tier") or "production"' in main_source
    assert 'profile.get("nextpnr_tool")' in main_source
    assert 'profile.get("device")' in main_source
    assert '"candidate_boards": _hem_reference_fpga_candidates(payload)' in main_source


def test_application_intelligence_fpga_shortlist_is_supabase_governed():
    root = Path(__file__).parents[1]
    migration = (root / "supabase" / "migrations" / "phase_20260815_application_intelligence_fpga_shortlist.sql").read_text(encoding="utf-8")
    assert "reference_fpga_candidate_boards" in migration
    assert "ulx3s_ecp5_45f" in migration
    assert "orangecrab_ecp5_85f" in migration
    assert "nvidia.domino.automotive_aero" in migration


def test_processor_ip_defaults_are_supabase_governed():
    migration = (Path(__file__).parents[1] / "supabase" / "migrations" / "phase_20260816_application_intelligence_processor_ip_policy.sql").read_text(encoding="utf-8")
    assert "processor_ip_policy" in migration
    assert "chiploop.application_intelligence.processor_ip_policy.v2" in migration
    assert "automatic_asic_deployment', 'asic_digital_ip" in migration
    assert "fpga_soft_cpu" in migration
    assert "asic_soc_cpu" in migration
    assert "pending_cpu_rtl" in migration
    assert "where name = 'Physical_AI_Loop' and user_id is null" in migration


def test_physical_ai_fpga_bitstream_uses_fpga_dashboard():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    stage_block = main_source.split('"fpga_bitstream": {', 1)[1].split('},', 1)[0]
    assert '"dashboard_stage": "fpga"' in stage_block
    assert '"dashboard_stage": "synthesis"' not in stage_block


def test_physical_ai_firmware_product_uses_supported_embedded_dashboard():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    stage_block = main_source.split('"firmware_product": {', 1)[1].split('},', 1)[0]
    assert '"dashboard_stage": "embedded"' in stage_block
    assert '"dashboard_stage": "firmware"' not in stage_block


def test_physical_ai_product_arch2rtl_requires_real_firmware_control_plane():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert "FIRMWARE CONTROL-PLANE CONTRACT (mandatory)" in main_source
    assert "NO COMMAND FALLBACK CONTRACT (mandatory" in main_source
    assert 'payload.get("allow_command_fallback") is True' in main_source
    assert "DIGITAL_SPEC_JSON.register_contract must describe the implemented registers and fields" in main_source
    assert "EXTERNAL-HOST CONTROL CONTRACT" in main_source
    assert 'firmware_mmio_modes = {"automatic", "fpga_onboard_cpu", "fpga_soft_cpu", "asic_soc"}' in main_source
    assert '"require_firmware_control_plane": bool(' in main_source
    assert "missing_concrete_firmware_register_map" in main_source
    assert "Supabase-backed Arch2RTL handoff has no concrete register map" in main_source
    assert '"_fail_fast_on_agent_error": True' in main_source


def test_external_host_physical_ai_plan_does_not_queue_mmio_firmware():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'firmware_inapplicable = deployment_architecture in {"fpga_external_host", "asic_companion", "asic_digital_ip"}' in main_source
    assert 'device_layer_role = "host_device_layer" if external_host else "embedded_firmware"' in main_source
    assert 'plan.append("firmware_product")' in main_source


def test_system_firmware_uses_supported_embedded_dashboard():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    stage_block = main_source.split('HEM_SYSTEM_RTL_STAGE_META:', 1)[1].split('HEM_SYSTEM_RTL_FIXED_POLICY:', 1)[0]
    assert '"System_Firmware": {"title": "HEM: System Firmware", "artifact": "system", "label": "Firmware", "stage": "embedded"' in stage_block
    assert '"stage": "firmware"' not in stage_block


def test_dashboard_heatmap_and_product_summary_recognize_platform_agent_lifecycle():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert '"ACTIVE AGENT:"' in main_source
    assert '"AGENT COMPLETED:"' in main_source
    assert '"AGENT FAILED:"' in main_source
    assert 'ACTIVE\\s+AGENT|AGENT\\s+(?:COMPLETED|FAILED)' in main_source


def test_physical_ai_hem_carries_fpga_simulation_lineage_into_product_chain():
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'automation_payload["fpga_bitstream_workflow_id"] = child_workflow_id' in main_source
    assert 'automation_payload["source_system_sim_workflow_id"] = child_workflow_id' in main_source
    assert '"source_system_sim_workflow_id": payload.get("source_system_sim_workflow_id") or payload.get("fpga_bitstream_workflow_id")' in main_source
    assert '"fpga_bitstream_workflow_id": common.get("fpga_bitstream_workflow_id")' in main_source


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
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
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
                "processor_ip_policy": TEST_PROCESSOR_POLICY,
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
                "processor_ip_policy": TEST_PROCESSOR_POLICY,
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
                "processor_ip_policy": TEST_PROCESSOR_POLICY,
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
    assert refinement["selected_mode"] == "fpga_external_host"
    assert refinement["firmware_gate"]["ready"] is False
    assert "fpga_onboard_cpu" in refinement["candidate_modes"]
    assert "fpga_external_host" in refinement["candidate_modes"]
    assert len(result["partition"]["jobs"]) == 5
    assert result["files"]["cpu_reference_results"]
    assert result["files"]["control_policy"]
    assert result["physics_execution"]["control_policy"]["format"] == "piecewise_linear_lut"
    assert len(result["physics_execution"]["cpu_reference"]["operating_points"]) == 5


def test_soft_cpu_beginner_defaults_resolve_to_balanced_reproducible_contract(tmp_path):
    result = run_physical_ai_workflow({
        "application": "intelligent_active_aerodynamics_controller",
        "physics_domain": "automotive_aerodynamics",
        "physics_model_id": "nvidia.domino.automotive_aero",
        "execution_mode": "architecture",
        "implementation_path": "fpga_prototype",
        "deployment_architecture": "fpga_soft_cpu",
        "soft_cpu_config": {"core": "automatic", "isa": "automatic", "bus": "automatic"},
        "processor_ip_policy": TEST_PROCESSOR_POLICY,
        "hem_enabled": False,
    }, str(tmp_path))
    cpu = result["soft_cpu"]
    assert cpu["core"] == "picorv32"
    assert cpu["isa"] == "rv32imc"
    assert cpu["target_triple"] == "riscv32imc-unknown-none-elf"
    assert cpu["bus"] == "wishbone"
    assert cpu["selection_mode"] == "automatic"
    assert result["partition"]["partition_phases"]["target_refinement"]["soft_cpu"] == cpu


def test_soft_cpu_advanced_override_validates_core_isa():
    cpu = resolve_soft_cpu_config({"core": "serv", "isa": "rv32i", "bus": "wishbone"}, deployment_architecture="fpga_soft_cpu", policy=TEST_PROCESSOR_POLICY)
    assert cpu["target_triple"] == "riscv32i-unknown-none-elf"
    assert cpu["selection_mode"] == "advanced_override"
    assert cpu["license"] == "ISC"
    with pytest.raises(ValueError, match="does not support"):
        resolve_soft_cpu_config({"core": "serv", "isa": "rv32imc"}, deployment_architecture="fpga_soft_cpu", policy=TEST_PROCESSOR_POLICY)


def test_asic_partition_records_explicit_soc_architecture(tmp_path):
    result = run_physical_ai_workflow(
        {
            "application": "intelligent_active_aerodynamics_controller",
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "execution_mode": "architecture",
            "implementation_path": "digital_ip_asic",
            "deployment_architecture": "asic_soc",
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
            "hem_enabled": False,
        },
        str(tmp_path),
    )

    refinement = result["partition"]["partition_phases"]["target_refinement"]
    assert refinement["status"] == "selected"
    assert refinement["selected_mode"] == "asic_soc"
    assert "asic_digital_ip" in refinement["candidate_modes"]
    assert refinement["asic_cpu"]["core"] == "picorv32"
    assert refinement["asic_cpu"]["bus"] == "apb"
    assert refinement["asic_cpu"]["integration_gate"]["status"] == "pending_cpu_rtl"


def test_automatic_asic_defaults_to_reusable_digital_ip(tmp_path):
    result = run_physical_ai_workflow({
        "physics_domain": "automotive_aerodynamics",
        "physics_model_id": "nvidia.domino.automotive_aero",
        "execution_mode": "architecture",
        "implementation_path": "digital_ip_asic",
        "deployment_architecture": "automatic",
        "processor_ip_policy": TEST_PROCESSOR_POLICY,
        "hem_enabled": False,
    }, str(tmp_path))
    refinement = result["partition"]["partition_phases"]["target_refinement"]
    assert refinement["selected_mode"] == "asic_digital_ip"
    assert refinement["status"] == "selected"
    assert refinement["asic_cpu"] is None


def test_automatic_fpga_uses_supabase_governed_deployment(tmp_path):
    result = run_physical_ai_workflow({
        "physics_domain": "automotive_aerodynamics",
        "physics_model_id": "nvidia.domino.automotive_aero",
        "execution_mode": "architecture",
        "implementation_path": "fpga_prototype",
        "deployment_architecture": "automatic",
        "processor_ip_policy": TEST_PROCESSOR_POLICY,
        "hem_enabled": False,
    }, str(tmp_path))
    requirements = result["requirements"]
    refinement = result["partition"]["partition_phases"]["target_refinement"]
    assert requirements["deployment_architecture_requested"] == "automatic"
    assert requirements["deployment_architecture"] == "fpga_external_host"
    assert refinement["selected_mode"] == "fpga_external_host"


def test_asic_cpu_advanced_override_and_validation():
    cpu = resolve_asic_cpu_config({"core": "vexriscv", "isa": "rv32imc", "bus": "axi4_lite", "clock_mhz": 200}, deployment_architecture="asic_soc", policy=TEST_PROCESSOR_POLICY)
    assert cpu["target_triple"] == "riscv32imc-unknown-none-elf"
    assert cpu["selection_mode"] == "advanced_override"
    assert cpu["clock_mhz"] == 200
    assert cpu["dft_scan_required"] is True
    with pytest.raises(ValueError, match="unsupported ASIC CPU bus"):
        resolve_asic_cpu_config({"core": "picorv32", "bus": "invalid"}, deployment_architecture="asic_soc", policy=TEST_PROCESSOR_POLICY)


def test_cpu_enabled_runs_require_supabase_policy():
    with pytest.raises(ValueError, match="Supabase processor_ip_policy"):
        resolve_soft_cpu_config({"core": "automatic"}, deployment_architecture="fpga_soft_cpu")
    with pytest.raises(ValueError, match="Supabase processor_ip_policy"):
        resolve_asic_cpu_config({"core": "automatic"}, deployment_architecture="asic_soc")


def test_processor_resolvers_use_supabase_policy_snapshot():
    policy = {
        "fpga_soft_cpu": {"default_core": "serv", "defaults": {"clock_mhz": 40, "instruction_memory_kib": 16, "data_memory_kib": 8, "interrupts": True, "uart": True, "debug": False}, "allowed_buses": ["wishbone"], "cores": {"serv": {"label": "Governed SERV", "license": "ISC", "profile": "minimum_area", "default_isa": "rv32i", "supported_isas": ["rv32i"], "default_bus": "wishbone", "estimated_logic_cells": 777, "estimated_bram_blocks": 5}}},
        "asic_soc_cpu": {"default_core": "vexriscv", "defaults": {"clock_mhz": 150, "boot_rom_kib": 16, "sram_kib": 64, "interrupts": True, "debug": False, "clock_gating": True, "dft_scan_required": True}, "allowed_buses": ["axi4_lite"], "cores": {"vexriscv": {"label": "Governed Vex", "license": "MIT", "profile": "performance", "default_isa": "rv32imc", "supported_isas": ["rv32imc"], "default_bus": "axi4_lite"}}, "integration_gate": {"cpu_rtl_required": True, "memory_macro_mapping_required": True, "complete_soc_synthesis_required": True, "default_status": "pending_cpu_rtl"}},
    }
    fpga = resolve_soft_cpu_config({"core": "automatic"}, deployment_architecture="fpga_soft_cpu", policy=policy)
    asic = resolve_asic_cpu_config({"core": "automatic"}, deployment_architecture="asic_soc", policy=policy)
    assert fpga["core"] == "serv" and fpga["estimated_reservation"]["logic_cells"] == 777
    assert asic["core"] == "vexriscv" and asic["bus"] == "axi4_lite"


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
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
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
                "processor_ip_policy": TEST_PROCESSOR_POLICY,
        },
        str(tmp_path),
    )
    assert result["status"] == "architecture_complete"
    assert result["loop"]["child_handoff"]["next_loop"] is None
    assert next(stage for stage in result["loop"]["stages"] if stage["id"] == "digital_design")["status"] == "not_requested"


def test_selected_agent_model_generates_rtl_ready_architecture(tmp_path, monkeypatch):
    _mock_upstream_model_planners(monkeypatch)
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
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
        },
        str(tmp_path),
    )
    assert calls
    assert calls[0][1]["agent_name"] == "Physical AI Architecture Agent"
    assert result["physics_execution"]["architecture"]["rtl_spec_text"].startswith("Create a synthesizable")
    assert result["physics_execution"]["architecture"]["top_module"] == "adaptive_aero_control_top"
    assert result["loop"]["child_handoff"]["rtl_spec_text"] == response["rtl_spec_text"]


def test_architecture_agent_repairs_json_syntax_and_persists_both_outputs(tmp_path, monkeypatch):
    _mock_upstream_model_planners(monkeypatch)
    response = {
        "product_name": "Active Aero Controller",
        "top_module": "adaptive_aero_control_top",
        "product_summary": "Safe controller.",
        "architecture_decisions": ["Firmware-mediated commands"],
        "blocks": ["command_guard"],
        "interfaces": ["Wishbone"],
        "safety_requirements": ["Safe fallback"],
        "rtl_spec_text": "Build a synthesizable bounded command controller.",
        "verification_goals": ["Verify fallback"],
    }
    outputs = iter(['{"product_name" "broken"}', json.dumps(response)])
    calls = []
    monkeypatch.setattr(
        architecture_agent,
        "complete_text",
        lambda prompt, **kwargs: calls.append((prompt, kwargs)) or next(outputs),
    )

    result = run_physical_ai_workflow(
        {
            "physics_domain": "automotive_aerodynamics",
            "physics_model_id": "nvidia.domino.automotive_aero",
            "implementation_target": "fpga",
            "execution_mode": "architecture",
            "implementation_path": "fpga_prototype",
            "generate_architecture_with_model": True,
            "model_policy": {"mode": "standard", "selected_model": "nvidia_nemotron"},
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
        },
        str(tmp_path),
    )

    assert len(calls) == 2
    assert "JSON ERROR:" in calls[1][0]
    assert result["physics_execution"]["architecture"]["top_module"] == "adaptive_aero_control_top"
    artifact_root = tmp_path / "physical_ai"
    assert (artifact_root / "model_generated_architecture_raw.txt").read_text(encoding="utf-8") == '{"product_name" "broken"}'
    assert json.loads((artifact_root / "model_generated_architecture_repaired_raw.txt").read_text(encoding="utf-8"))["product_name"] == "Active Aero Controller"


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
    assert '"asic_cpu_config": result.get("asic_cpu") or {}' in main
    assert "ASIC CPU IP INTEGRATION CONTRACT" in main
    assert 'hardware_model_transport = deployment_architecture != "fpga_soft_cpu"' in main
    assert "SOFT-CPU MODEL TRANSPORT CONTRACT" in main
    assert '"cpu_ip_integration_gate": (payload.get("asic_cpu_config") or {}).get("integration_gate")' in main
    assert '"software_goal": f"Build the host control' in main
    assert '"Device Layer / Firmware"' in main
    assert 'device_layer_role = "host_device_layer" if external_host else "embedded_firmware"' in main
    assert 'target_triple = "x86_64-unknown-linux-gnu"' in main
    assert 'payload.get("external_host_config") or payload.get("host_toolchain") or compute_host' in main
    assert 'External host architecture {host_arch!r} requires an explicit Rust target_triple' in main
    assert 'deployment == "fpga_onboard_cpu"' in main
    assert 'compute_host.get("hard_cpu")' in main
    assert "no CPU architecture is inferred" in main
    assert 'automation_payload["fpga_source_workflow_id"] = child_workflow_id' in main
    assert 'payload.get("fpga_source_workflow_id") or source_arch2rtl' in main
    assert '"_fail_fast_on_agent_error": True' in main
    assert '"rtl_source_mode": "from_workflow"' in main
    assert "direct Arch2RTL hydration deferred to Embedded Digital RTL Handoff Ingest Agent" in main
    assert "def _hem_child_failure_summary" in main
    assert 'filename="system_app_failure.json"' in main
    assert 'dashboard_stage=next_meta.get("stage") or next_template.lower()' in main
    assert 'nested_status = _hem_run_status' in main
    assert 'nested_status != "completed"' in main
    assert 'Preserve that deeper Supabase state' in main


def test_external_host_requires_complete_interface_plan_before_workflow_creation():
    from physical_ai.interface_contract import validate_external_host_interface_plan

    with pytest.raises(ValueError, match="interface plan before RTL generation"):
        validate_external_host_interface_plan({})

    validate_external_host_interface_plan({
        "protocol": "spi", "role": "fpga_peripheral", "clock_mhz": 10,
        "data_width_bits": 8, "framing": "register_command_response",
        "flow_control": "chip_select_and_status", "interrupt_signaling": "optional_gpio",
        "register_access": "addressed_read_write",
    })


def test_external_host_rejects_unqualified_transport():
    from physical_ai.interface_contract import validate_external_host_interface_plan

    with pytest.raises(ValueError, match="not yet qualified"):
        validate_external_host_interface_plan({
            "protocol": "pcie", "role": "fpga_peripheral", "clock_mhz": 100,
            "data_width_bits": 32, "framing": "tlp", "flow_control": "credits",
            "interrupt_signaling": "msi", "register_access": "bar_mmio",
        })


@pytest.mark.parametrize("field,value", [
    ("clock_mhz", 0),
    ("clock_mhz", 101),
    ("data_width_bits", 16),
    ("role", "fpga_controller"),
    ("framing", "raw_stream"),
])
def test_external_host_rejects_unimplemented_spi_contract_variants(field, value):
    from physical_ai.interface_contract import validate_external_host_interface_plan

    plan = {
        "protocol": "spi", "role": "fpga_peripheral", "clock_mhz": 10,
        "data_width_bits": 8, "framing": "register_command_response",
        "flow_control": "chip_select_and_status", "interrupt_signaling": "optional_gpio",
        "register_access": "addressed_read_write",
    }
    plan[field] = value
    with pytest.raises(ValueError):
        validate_external_host_interface_plan(plan)


def test_physical_ai_parent_status_is_not_overwritten_by_downstream_hem_failure():
    main = open("main.py", encoding="utf-8").read()
    continuation = main.split(
        "def _hem_continue_physical_ai_after_success", 1
    )[1].split("def execute_physical_ai_motor_control_background", 1)[0]

    assert 'append_log_workflow(root_workflow_id, message, status="failed"' not in continuation
    assert 'append_log_run(root_run_id, message, status="failed")' not in continuation
    assert '_hem_update_run_record' in continuation
    assert 'status="failed"' in continuation
    assert "the child and HEM run remain the" in continuation


def test_physical_ai_reuses_existing_firmware_and_software_collateral_contracts():
    main = open("main.py", encoding="utf-8").read()
    firmware_ingest = open("agents/embedded/embedded_digital_handoff_ingest_agent.py", encoding="utf-8").read()
    firmware_package = open("agents/system/system_software_handoff_package_agent.py", encoding="utf-8").read()
    software_ingest = open("agents/system/system_software_handoff_ingest_agent.py", encoding="utf-8").read()

    assert '"system_rtl_workflow_id": source_arch2rtl' in main
    assert '"from_workflow_id": source_arch2rtl' in main
    assert '"rtl_source_mode": "from_workflow"' in main
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
            "processor_ip_policy": TEST_PROCESSOR_POLICY,
        },
        str(tmp_path),
    )
    assert result["physics_model"]["name"] == "Supabase governed PMSM"
