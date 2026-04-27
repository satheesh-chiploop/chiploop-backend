import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_contract.tool_validation import validate_tool_availability
from studio_factory.custom_agent_export import build_custom_agent_registry_patch, write_custom_agent_patch
from studio_factory.generate_agent import run_factory
from studio_factory.models import AgentFactoryRequest
from studio_factory.planner import plan_factory_request
from studio_factory.validator import validate_factory_plan
from studio_planner.models import AgentPlanRequest
from studio_planner.planner import plan_agent


def test_agent_planner_exact_match_detection():
    result = plan_agent(
        AgentPlanRequest(
            natural_language_request="Digital RTL Agent",
            domain="digital",
            loop_type="digital",
        )
    )

    assert result.recommendation == "reuse_existing"
    assert result.exact_matches[0].name == "Digital RTL Agent"


def test_agent_planner_similar_match_detection():
    result = plan_agent(
        AgentPlanRequest(
            natural_language_request="Generate RTL compile repair feedback",
            domain="digital",
            loop_type="digital",
        )
    )

    assert result.similar_matches
    assert result.recommendation in {"compose_existing", "extend_existing"}


def test_factory_exact_match_stops_generation():
    plan = plan_factory_request(
        AgentFactoryRequest(
            name="Digital RTL Agent",
            natural_language_request="Digital RTL Agent",
            domain="digital",
            loop_type="digital",
        )
    )

    assert plan.decision == "reuse_existing"
    assert plan.exact_match == "Digital RTL Agent"
    assert not plan.files_to_generate


def test_factory_create_new_plan_json_serializable():
    plan = plan_factory_request(
        AgentFactoryRequest(
            name="System Timing Waiver Agent",
            natural_language_request="Create timing waiver summaries for system signoff review.",
            domain="physical_design",
            loop_type="system",
            required_skills=["sta_constraint_generation", "artifact_publish"],
            required_tools=["python", "supabase", "openroad"],
            allow_extension=True,
        )
    )

    assert plan.decision in {"create_new", "create_new_variant"}
    assert plan.files_to_generate
    assert json.dumps(plan.to_dict())


def test_factory_dry_run_writes_no_files(tmp_path):
    result = run_factory(
        AgentFactoryRequest(
            name="System Timing Waiver Agent",
            natural_language_request="Create timing waiver summaries for system signoff review.",
            domain="physical_design",
            loop_type="system",
            allow_extension=True,
        ),
        dry_run=True,
        output_dir="generated_studio_agents",
    )

    assert result.ok
    assert result.dry_run
    assert result.written_files == []


def test_factory_write_mode_safe_output_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run_factory(
        AgentFactoryRequest(
            name="System Timing Waiver Agent",
            natural_language_request="Create timing waiver summaries for system signoff review.",
            domain="physical_design",
            loop_type="system",
            required_skills=["sta_constraint_generation", "artifact_publish"],
            allow_extension=True,
        ),
        dry_run=False,
        output_dir=".",
    )

    assert result.ok
    assert result.written_files
    assert all(path.startswith(("generated_studio_agents", "generated_patches")) for path in result.written_files)


def test_factory_registry_patch_yaml_is_json_compatible():
    plan = plan_factory_request(
        AgentFactoryRequest(
            name="System Timing Waiver Agent",
            natural_language_request="Create timing waiver summaries for system signoff review.",
            domain="physical_design",
            loop_type="system",
            allow_extension=True,
        )
    )

    assert plan.registry_patch is not None
    assert json.loads(plan.registry_patch.content)["agents"]
    assert validate_factory_plan(plan) == []


def test_factory_unsafe_path_rejection():
    plan = plan_factory_request(
        AgentFactoryRequest(
            name="Unsafe Path Agent",
            natural_language_request="Create an unsafe path test agent.",
            domain="system",
            loop_type="system",
            allow_extension=True,
        )
    )
    plan.files_to_generate[0].path = "agents/unsafe_agent.py"

    errors = validate_factory_plan(plan)

    assert any("unsafe generated file path" in error for error in errors)


def test_tool_availability_validation_is_report_only():
    result = validate_tool_availability("registry")

    assert result["tool_count"] >= 1
    assert "tools" in result
    assert any(tool["name"] == "python" and tool["available"] for tool in result["tools"])


def test_custom_agent_export_builds_review_patch():
    patch = build_custom_agent_registry_patch(
        [
            {
                "agent_name": "Example Custom Review Agent",
                "domain": "custom",
                "loop_type": "custom",
            }
        ]
    )

    assert patch["review_required"]
    assert patch["agents"][0]["entrypoint"] == "AGENT_REGISTRY:Example Custom Review Agent"
    assert patch["agents"][0]["execution_mode"] == "legacy_adapter"


def test_custom_agent_export_rejects_unsafe_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    patch = build_custom_agent_registry_patch([{"agent_name": "Unsafe Export Agent"}])

    with pytest.raises(ValueError):
        write_custom_agent_patch(patch, "registry/custom_agents.yaml")
