import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_contract.registry import dry_run_validate, load_registry, validate_registry


def test_studio_registry_loads_all_contract_files():
    registry = load_registry("registry")

    assert len(registry.agents) >= 100
    assert "Digital RTL Agent" in registry.agents
    assert registry.agents["Digital RTL Agent"].entrypoint == "agents.digital.digital_rtl_agent:run_agent"
    assert registry.agents["Digital RTL Agent"].execution_mode == "native"
    assert registry.skills
    assert registry.tools
    assert registry.hooks
    assert registry.workflows


def test_studio_registry_validates_references():
    registry = load_registry("registry")
    ok, errors = validate_registry(registry)

    assert ok, errors


def test_studio_registry_dry_run_validation_mode():
    result = dry_run_validate("registry")

    assert result["ok"], result["errors"]
    assert result["counts"]["agents"] == 151
    assert result["counts"]["skills"] == 12
    assert result["counts"]["tools"] == 11
    assert result["counts"]["hooks"] == 6
