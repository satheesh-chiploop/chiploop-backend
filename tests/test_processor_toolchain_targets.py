import os

import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from physical_ai.asic_cpu import resolve_asic_cpu_config
from physical_ai.soft_cpu import resolve_soft_cpu_config
from physical_ai.toolchain_targets import resolve_rust_toolchain
from agents.embedded.embedded_elf_build_agent import _cargo_target_argument, _cargo_target_directory, _default_cargo_config


def test_legacy_riscv_isa_resolves_without_cpu_name_rules():
    resolved = resolve_rust_toolchain({}, "rv64gc", default_abi="lp64d")

    assert resolved["target_triple"] == "riscv64gc-unknown-none-elf"
    assert resolved["target_abi"] == "lp64d"


def test_non_riscv_cpu_uses_policy_declared_per_isa_toolchain():
    resolved = resolve_rust_toolchain(
        {
            "toolchains": {
                "armv7em": {
                    "target_triple": "thumbv7em-none-eabihf",
                    "abi": "eabihf",
                    "features": ["v7", "vfp4"],
                }
            }
        },
        "armv7em",
    )

    assert resolved == {
        "target_triple": "thumbv7em-none-eabihf",
        "target_abi": "eabihf",
        "compiler_features": ["v7", "vfp4"],
        "custom_target_json": None,
    }


def test_unknown_cpu_family_requires_explicit_target_contract():
    with pytest.raises(ValueError, match="must declare toolchains.vendor32.target_triple"):
        resolve_rust_toolchain({}, "vendor32")


def test_custom_rust_target_uses_json_path_but_stable_cargo_output_directory(tmp_path):
    target = "toolchains/vendor32.json"
    target_path = tmp_path / target
    target_path.parent.mkdir()
    target_path.write_text("{}", encoding="utf-8")

    assert _cargo_target_argument(str(tmp_path), target) == str(target_path)
    assert _cargo_target_directory(target) == "vendor32"


def test_custom_rust_target_cannot_escape_workflow(tmp_path):
    outside = tmp_path.parent / "outside-target.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="contained within the workflow"):
        _cargo_target_argument(str(tmp_path), "../outside-target.json")


def test_cargo_config_applies_policy_compiler_features_to_selected_target():
    config = _default_cargo_config("thumbv7em-none-eabihf", ["v7", "+vfp4", "-soft-float"])

    assert '[target."thumbv7em-none-eabihf"]' in config
    assert "target-feature=+v7" in config
    assert "target-feature=+vfp4" in config
    assert "target-feature=-soft-float" in config


@pytest.mark.parametrize("kind", ["soft", "asic"])
def test_processor_resolvers_propagate_advanced_toolchain_metadata(kind):
    core = {
        "label": "Policy CPU",
        "license": "policy",
        "profile": "advanced",
        "default_isa": "armv7em",
        "supported_isas": ["armv7em"],
        "default_bus": "axi4_lite",
        "toolchains": {
            "armv7em": {
                "target_triple": "thumbv7em-none-eabihf",
                "abi": "eabihf",
                "compiler_features": ["v7", "vfp4"],
            }
        },
        "estimated_logic_cells": 1000,
        "estimated_bram_blocks": 4,
    }
    policy = {
        "schema": "policy-test",
        "fpga_soft_cpu": {
            "default_core": "policy_cpu",
            "defaults": {"clock_mhz": 50, "instruction_memory_kib": 16, "data_memory_kib": 16},
            "allowed_buses": ["axi4_lite"],
            "cores": {"policy_cpu": core},
        },
        "asic_soc_cpu": {
            "default_core": "policy_cpu",
            "defaults": {"clock_mhz": 100, "boot_rom_kib": 16, "sram_kib": 64},
            "allowed_buses": ["axi4_lite"],
            "cores": {"policy_cpu": core},
            "integration_gate": {},
        },
    }

    if kind == "soft":
        resolved = resolve_soft_cpu_config({}, deployment_architecture="fpga_soft_cpu", policy=policy)
    else:
        resolved = resolve_asic_cpu_config({}, deployment_architecture="asic_soc", policy=policy)

    assert resolved["target_triple"] == "thumbv7em-none-eabihf"
    assert resolved["target_abi"] == "eabihf"
    assert resolved["compiler_features"] == ["v7", "vfp4"]
