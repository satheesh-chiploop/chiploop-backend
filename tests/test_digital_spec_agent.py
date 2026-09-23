import os
import json
import copy
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_spec_agent as spec_agent
from agents.digital.feature_contract_compiler import compile_feature_contracts


def test_malformed_outer_json_recovery_preserves_nested_feature_contracts():
    # The outer object is truncated, but its hierarchy object is complete and
    # contains later root-like fields emitted at the wrong nesting level.
    raw = json.dumps({
        "top_module": {"name": "top", "ports": []},
        "modules": [{"name": "child", "ports": []}],
        "top_level_connections": [{"top_port": "clk", "connected_to": ["child.clk"]}],
        "inter_module_signals": [{"name": "x", "width": 1, "source": "child.x", "destinations": ["top.x"]}],
        "signal_ownership": [{"signal": "x", "owner": "child.x"}],
        "feature_contracts": [{"id": "observable", "stimulus": {"clk": 1}, "expected": {"done": 1}}],
    })
    malformed = '{"design_name":"top","hierarchy":' + raw

    parsed = spec_agent._parse_llm_json_object(malformed)

    assert parsed["feature_contracts"][0]["id"] == "observable"


def test_malformed_final_feature_array_prefers_repaired_outer_contract():
    contract = {
        "design_name": "top",
        "hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [{"name": "child", "ports": []}]},
        "top_level_connections": [{"top_port": "clk", "connected_to": ["child.clk"]}],
        "inter_module_signals": [{"name": "x", "width": 1, "source": "child.x", "destinations": ["top.x"]}],
        "signal_ownership": [{"signal": "x", "owner": "child.x"}],
        "feature_contracts": [{"id": "late_feature", "stimulus": {"clk": 1}, "expected": {"done": 1}}],
    }
    raw = json.dumps(contract)
    malformed = raw[:-2] + "}"  # close the final feature as an object instead of closing its array/root

    parsed = spec_agent._parse_llm_json_object(malformed)

    assert parsed["design_name"] == "top"
    assert parsed["feature_contracts"][0]["id"] == "late_feature"


def test_memory_observability_accepts_unambiguous_functional_wrapper_name():
    spec = {
        "memory_macros": [{
            "name": "payload_bram", "ports": {
                "clk": "clk", "csb": "csb", "we": "we", "addr": "addr", "din": "din", "dout": "dout",
            },
        }],
        "hierarchy": {
            "top_module": {"name": "top", "ports": []},
            "modules": [
                {"name": "payload_fifo_wrapper", "ports": [
                    {"name": name, "direction": "output" if name == "dout" else "input", "width": 1}
                    for name in ("clk", "csb", "we", "addr", "din", "dout")
                ]},
                {"name": "consumer", "ports": [{"name": "mem_dout", "direction": "input", "width": 1}]},
            ],
        },
        "inter_module_signals": [{
            "name": "read_data", "width": 1, "source": "payload_fifo_wrapper.dout",
            "destinations": ["consumer.mem_dout"],
        }],
        "top_level_connections": [],
    }

    spec_agent._validate_required_memory_observability(spec)


def test_mandatory_no_fallback_contract_rejects_structural_fallback_interface():
    spec = {
        "name": "top",
        "ports": [{"name": "fallback_active", "direction": "output", "width": 1}],
        "register_contract": {"registers": [{
            "name": "CONTROL", "fields": [{"name": "safe_actuator_cmd"}],
        }]},
    }
    with pytest.raises(ValueError, match="fallback_active.*safe_actuator_cmd|safe_actuator_cmd.*fallback_active"):
        spec_agent._validate_no_command_fallback_contract(
            spec, "NO COMMAND FALLBACK CONTRACT (mandatory; overrides conflicting generated prose): inhibit validity."
        )


def test_internal_config_feature_is_projected_through_mmio_register_contract():
    spec = {
        "ports": [
            {"name": "mmio_addr", "direction": "input", "width": 8},
            {"name": "mmio_wdata", "direction": "input", "width": 32},
            {"name": "mmio_write", "direction": "input", "width": 1},
            {"name": "mmio_read", "direction": "input", "width": 1},
            {"name": "mmio_valid", "direction": "input", "width": 1},
            {"name": "rsp_valid", "direction": "input", "width": 1},
            {"name": "rsp_ready", "direction": "output", "width": 1},
        ],
        "register_contract": {"registers": [{
            "name": "CTRL", "address": 4, "access": "rw",
            "fields": [{"name": "enable", "lsb": 2, "msb": 2, "access": "rw"}],
        }]},
        "feature_contracts": [{
            "id": "accept_response", "description": "Accept a response when enabled.",
            "stimulus": {"cfg_enable": 1, "rsp_valid": 1, "rsp_ready": 1},
            "expected": {"rsp_ready": 1}, "within_cycles": 1,
        }],
    }

    spec_agent._project_internal_feature_stimulus_to_register_bus(spec, "flat")

    steps = spec["feature_contracts"][0]["stimulus"]["steps"]
    assert steps[0]["signals"] == {
        "mmio_addr": 4, "mmio_wdata": 4, "mmio_write": 1,
        "mmio_valid": 1, "mmio_read": 0,
    }
    assert steps[1]["signals"] == {
        "rsp_valid": 1, "mmio_write": 0, "mmio_valid": 0, "mmio_read": 0,
    }
    compiled = compile_feature_contracts(spec, spec["ports"])
    assert compiled[0]["executable"] is True
    assert compiled[0]["stimulus_cycles"] == 2


def test_internal_config_projection_preserves_empty_wait_step_shape():
    spec = {
        "ports": [
            _port("mmio_addr", "input", 8), _port("mmio_wdata", "input", 32),
            _port("mmio_write", "input"), _port("done", "output"),
        ],
        "register_contract": {"registers": [{
            "name": "CTRL", "address": 0, "access": "rw",
            "fields": [{"name": "enable", "lsb": 0, "msb": 0, "access": "rw"}],
        }]},
        "feature_contracts": [{
            "id": "enable_then_wait",
            "stimulus": {"steps": [
                {"signals": {"cfg_enable": 1}, "cycles": 1},
                {"signals": {}, "cycles": 3},
            ]},
            "expected": {"done": 1},
        }],
    }

    spec_agent._project_internal_feature_stimulus_to_register_bus(spec, "flat")

    steps = spec["feature_contracts"][0]["stimulus"]["steps"]
    assert steps[-1] == {"signals": {}, "cycles": 3}
    assert compile_feature_contracts(spec, spec["ports"])[0]["executable"] is True


def test_internal_config_projection_accepts_mmio_we_alias():
    spec = {
        "ports": [
            {"name": "mmio_addr", "direction": "input", "width": 8},
            {"name": "mmio_wdata", "direction": "input", "width": 32},
            {"name": "mmio_we", "direction": "input", "width": 1},
            {"name": "done", "direction": "output", "width": 1},
        ],
        "register_contract": {"registers": [{
            "name": "CTRL", "address": 0, "access": "rw",
            "fields": [{"name": "enable", "lsb": 0, "msb": 0, "access": "rw"}],
        }]},
        "feature_contracts": [{
            "id": "enable", "description": "Enable through firmware.",
            "stimulus": {"cfg_enable": 1}, "expected": {"done": {"eq": 1}},
        }],
    }

    spec_agent._project_internal_feature_stimulus_to_register_bus(spec, "flat")

    steps = spec["feature_contracts"][0]["stimulus"]["steps"]
    assert steps[0]["signals"] == {"mmio_addr": 0, "mmio_wdata": 1, "mmio_we": 1}
    assert steps[1]["signals"]["mmio_we"] == 0


def test_internal_config_projection_canonicalizes_reversed_field_bounds():
    spec = {
        "ports": [
            {"name": "mmio_addr", "direction": "input", "width": 8},
            {"name": "mmio_wdata", "direction": "input", "width": 32},
            {"name": "mmio_we", "direction": "input", "width": 1},
            {"name": "done", "direction": "output", "width": 1},
        ],
        "register_contract": {"registers": [{
            "name": "LIMIT", "address": 4, "access": "rw",
            "fields": [{"name": "timeout_cycles", "lsb": 15, "msb": 0, "access": "rw"}],
        }]},
        "feature_contracts": [{
            "id": "timeout", "description": "Program timeout.",
            "stimulus": {"cfg_timeout_cycles": 9}, "expected": {"done": {"eq": 1}},
        }],
    }

    spec_agent._project_internal_feature_stimulus_to_register_bus(spec, "flat")

    field = spec["register_contract"]["registers"][0]["fields"][0]
    assert (field["lsb"], field["msb"]) == (0, 15)
    assert spec["feature_contracts"][0]["stimulus"]["steps"][0]["signals"]["mmio_wdata"] == 9


def test_feature_validation_reports_exact_unresolved_bindings():
    spec = {
        "name": "top",
        "ports": [{"name": "done", "direction": "output", "width": 1}],
        "feature_contracts": [{
            "id": "bad_internal", "description": "Do not drive internal state.",
            "stimulus": {"internal_age": 4}, "expected": {"done": {"eq": 1}},
        }],
    }

    with pytest.raises(ValueError, match="Unresolved bindings: internal_age"):
        spec_agent._validate_spec_contract(spec, "flat", require_feature_contracts=True)


def test_unique_top_input_alias_fans_out_to_orphan_child_input():
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "ports": [{"name": "req_ready", "direction": "input", "width": 1}]},
            "modules": [{"name": "core", "ports": [{"name": "model_req_ready", "direction": "input", "width": 1}]}],
        },
        "top_level_connections": [{"top_port": "req_ready", "connected_to": ["transport.req_ready"]}],
        "inter_module_signals": [],
    }

    spec_agent._connect_unique_top_input_aliases(spec)

    assert spec["top_level_connections"][0]["connected_to"] == ["transport.req_ready", "core.model_req_ready"]


def test_generic_single_token_top_input_is_not_used_as_suffix_alias():
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "ports": [{"name": "ready", "direction": "input", "width": 1}]},
            "modules": [{"name": "core", "ports": [{"name": "request_ready", "direction": "input", "width": 1}]}],
        },
        "top_level_connections": [], "inter_module_signals": [],
    }

    spec_agent._connect_unique_top_input_aliases(spec)

    assert spec["top_level_connections"] == []


def test_fpga_inferred_memory_wrapper_internalizes_primitive_pins():
    module = {
        "name": "history_store",
        "description": "Technology-neutral memory storage wrapper",
        "functionality": "Owns storage mapped to native block RAM.",
        "behavior_rules": ["Implement storage as an inferred memory."],
        "ports": [
            {"name": "mem_addr", "direction": "output", "width": 6},
            {"name": "mem_din", "direction": "output", "width": 32},
            {"name": "mem_dout", "direction": "input", "width": 32},
            {"name": "store_addr", "direction": "input", "width": 6},
            {"name": "store_wdata", "direction": "input", "width": 32},
            {"name": "store_rdata", "direction": "output", "width": 32},
        ],
        "must_receive": ["mem_dout", "store_addr", "store_wdata"],
        "must_drive": ["mem_addr", "mem_din", "store_rdata"],
    }
    spec = {"hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [module]}}

    spec_agent._internalize_fpga_inferred_memory_interfaces(spec, "FPGA MEMORY CONTRACT (mandatory)")

    assert {port["name"] for port in module["ports"]} == {"store_addr", "store_wdata", "store_rdata"}
    assert module["memory_implementation"]["kind"] == "fpga_bram"
    assert module["memory_implementation"]["depth"] == 64
    assert module["memory_implementation"]["data_width"] == 32
    assert "mem_dout" not in module["must_receive"]


def test_fpga_inferred_memory_detects_conventional_read_write_port_names():
    module = {
        "name": "application_history_wrapper",
        "description": "Technology-neutral memory wrapper for history storage",
        "functionality": "Own storage mapped to native block RAM.",
        "behavior_rules": ["Implement storage as an inferred memory."],
        "ports": [
            _port("write_en", "input"), _port("read_en", "input"),
            _port("write_addr", "input", 8), _port("read_addr", "input", 8),
            _port("write_data", "input", 64), _port("read_data", "output", 64),
        ],
    }
    spec = {"hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [module]}}

    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert module["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 256, "addr_width": 8, "data_width": 64,
        "technology_binding": "technology_neutral_inferred_memory",
    }


def test_fpga_inferred_memory_internalizes_application_prefixed_macro_pin_group():
    module = {
        "name": "history_buffer",
        "description": "Technology-neutral history memory wrapper",
        "functionality": "Own storage mapped to native block RAM.",
        "behavior_rules": ["Implement storage as an inferred memory."],
        "ports": [
            _port("write_en", "input"), _port("write_addr", "input", 8),
            _port("write_data", "input", 64), _port("read_addr", "input", 8),
            _port("read_data", "output", 64),
            _port("hist_web", "output"), _port("hist_addr", "output", 8),
            _port("hist_din", "output", 64), _port("hist_dout", "input", 64),
        ],
        "must_drive": ["read_data", "hist_web", "hist_addr", "hist_din"],
        "must_receive": ["write_en", "write_addr", "write_data", "read_addr", "hist_dout"],
    }
    spec = {"hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [module]}}

    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert {port["name"] for port in module["ports"]} == {
        "write_en", "write_addr", "write_data", "read_addr", "read_data",
    }
    assert not {"hist_web", "hist_addr", "hist_din", "hist_dout"}.intersection(
        module["must_drive"] + module["must_receive"]
    )


def test_fpga_inferred_memory_internalizes_active_low_macro_pin_group():
    module = {
        "name": "payload_memory_wrapper",
        "description": "Technology-neutral memory wrapper mapped to native block RAM.",
        "functionality": "Own inferred bulk storage.",
        "behavior_rules": ["Implement storage as an inferred memory."],
        "ports": [
            _port("write_en", "input"), _port("write_data", "input", 32),
            _port("read_data", "output", 32),
            _port("hist_csb_n", "output"), _port("hist_we_n", "output"),
            _port("hist_addr", "output", 6), _port("hist_din", "output", 32),
            _port("hist_dout", "input", 32),
        ],
    }
    spec = {"hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [module]}}

    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert {port["name"] for port in module["ports"]} == {"write_en", "write_data", "read_data"}


def test_fpga_memory_internalization_preserves_hybrid_module_application_ports():
    module = {
        "name": "sensor_ingest_and_history",
        "description": "Sensor ingress with technology-neutral history storage",
        "functionality": "Accept sensor samples and own inferred native block RAM storage.",
        "behavior_rules": ["Implement history storage as an inferred memory."],
        "ports": [
            _port("sensor_valid", "input"), _port("sensor_ready", "output"),
            _port("sensor_data", "input", 32),
            _port("history_csb", "output"), _port("history_web", "output"),
            _port("history_addr", "output", 8), _port("history_din", "output", 64),
            _port("history_dout", "input", 64),
        ],
        "must_drive": ["sensor_ready", "history_csb", "history_web", "history_addr", "history_din"],
        "must_receive": ["sensor_valid", "sensor_data", "history_dout"],
    }
    spec = {"hierarchy": {
        "top_module": {"name": "top", "ports": [
            _port("sensor_valid", "input"), _port("sensor_ready", "output"),
            _port("sensor_data", "input", 32),
            _port("history_dout", "input", 64),
        ]},
        "modules": [module],
    }}

    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert {port["name"] for port in spec["hierarchy"]["top_module"]["ports"]} == {
        "sensor_valid", "sensor_ready", "sensor_data",
    }
    assert {port["name"] for port in module["ports"]} == {
        "sensor_valid", "sensor_ready", "sensor_data",
    }
    assert module["functionality"].startswith("Accept sensor samples")
    assert "inferred memory" in module["functionality"]

    once = copy.deepcopy(spec)
    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )
    assert spec == once


def test_explicit_block_ram_wrapper_does_not_depend_on_port_prefix_grammar():
    module = {
        **_module("payload_store"),
        "description": "Technology-neutral memory wrapper for block-RAM-mappable bulk storage.",
        "functionality": "FIFO storage without flattened registers.",
        "behavior_rules": ["Instantiate storage compatible with native block RAM."],
        "ports": [
            _port("channel_write_en", "input"), _port("channel_addr", "input", 8),
            _port("channel_push_data", "input", 64),
            _port("channel_pop_data", "output", 64), _port("channel_full", "output"),
        ],
    }
    spec = {"hierarchy": {"top_module": _module("top"), "modules": [module]}}

    spec_agent._internalize_fpga_inferred_memory_interfaces(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert module["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 256, "addr_width": 8, "data_width": 64,
        "technology_binding": "technology_neutral_inferred_memory",
    }
    assert {port["name"] for port in module["ports"]} == {
        "channel_write_en", "channel_addr", "channel_push_data", "channel_pop_data", "channel_full",
    }


def test_child_only_inferred_memory_feature_routes_to_structural_validation():
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "ports": [_port("done", "output")]},
            "modules": [{
                **_module("storage"),
                "memory_implementation": {"kind": "fpga_bram"},
                "ports": [
                    _port("write_en", "input"), _port("write_data", "input", 32),
                    _port("full", "output"),
                ],
            }],
        },
        "feature_contracts": [
            {
                "id": "bulk_storage_structure",
                "description": "Bulk storage uses the inferred-memory wrapper.",
                "stimulus": {"write_en": 1, "write_data": 7},
                "expected": {"full": {"min": 0, "max": 1}},
            },
            {
                "id": "external_done",
                "description": "Product completion is externally observable.",
                "stimulus": {}, "expected": {"done": 1},
            },
        ],
    }

    out = spec_agent._route_internal_memory_features_to_structural_requirements(spec, "hierarchical")

    assert [feature["id"] for feature in out["feature_contracts"]] == ["external_done"]
    assert out["structural_requirements"] == [{
        "id": "bulk_storage_structure",
        "description": "Bulk storage uses the inferred-memory wrapper.",
        "kind": "fpga_inferred_memory_structure",
        "modules": ["storage"],
        "verification": "deterministic_fpga_memory_contract",
        "source": "feature_contract",
    }]


def test_internal_signal_feature_is_not_routed_without_memory_provenance():
    spec = {
        "hierarchy": {
            "top_module": {"name": "top", "ports": [_port("done", "output")]},
            "modules": [{**_module("core"), "ports": [_port("internal_state", "output", 8)]}],
        },
        "feature_contracts": [{
            "id": "bad_hidden_state", "stimulus": {}, "expected": {"internal_state": 3},
        }],
    }

    out = spec_agent._route_internal_memory_features_to_structural_requirements(spec, "hierarchical")

    assert out["feature_contracts"][0]["id"] == "bad_hidden_state"
    assert "structural_requirements" not in out


def test_fpga_inferred_wrapper_removes_stale_macro_prose_and_accidental_top_ports():
    module = {
        "name": "history_wrapper", "description": "Technology-neutral memory wrapper",
        "functionality": "Instantiate the SRAM macro for FPGA block RAM mapping.",
        "responsibilities": ["Instantiate the required SRAM macro cell using the declared instance name."],
        "behavior_rules": ["The wrapper shall connect exactly to the declared SRAM macro name and instance."],
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "history_csb", "direction": "input", "width": 1},
            {"name": "history_we", "direction": "input", "width": 1},
            {"name": "history_addr", "direction": "input", "width": 6},
            {"name": "history_din", "direction": "input", "width": 32},
            {"name": "history_dout", "direction": "output", "width": 32},
        ],
    }
    spec = {"hierarchy": {
        "top_module": {"name": "top", "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "history_dout", "direction": "input", "width": 32},
        ]},
        "modules": [module],
    }}

    spec_agent._internalize_fpga_inferred_memory_interfaces(spec, "FPGA MEMORY CONTRACT (mandatory)")

    assert {port["name"] for port in spec["hierarchy"]["top_module"]["ports"]} == {"clk"}
    assert "history_csb" not in {port["name"] for port in module["ports"]}
    assert all("macro" not in item.lower() for item in module["responsibilities"] + module["behavior_rules"])
    assert module["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 64, "addr_width": 6, "data_width": 32,
        "technology_binding": "technology_neutral_inferred_memory",
    }


def test_fpga_wrapper_keeps_primitive_pins_for_declared_macro_interface():
    ports = [
        {"name": "mem_addr", "direction": "output", "width": 6},
        {"name": "mem_din", "direction": "output", "width": 32},
        {"name": "mem_dout", "direction": "input", "width": 32},
        {"name": "store_addr", "direction": "input", "width": 6},
        {"name": "store_rdata", "direction": "output", "width": 32},
    ]
    module = {
        "name": "history_store", "description": "Memory storage wrapper",
        "functionality": "Storage wrapper", "behavior_rules": ["Instantiate memory macro."], "ports": ports,
    }
    spec = {
        "hierarchy": {"top_module": {"name": "top", "ports": []}, "modules": [module]},
        "memory_macros": [{"name": "ram", "ports": {"addr": "mem_addr", "din": "mem_din", "dout": "mem_dout"}}],
    }

    spec_agent._internalize_fpga_inferred_memory_interfaces(spec, "FPGA MEMORY CONTRACT (mandatory)")

    assert module["ports"] == ports
    assert "memory_implementation" not in module


def test_unknown_internal_feature_signal_is_not_silently_removed():
    spec = {
        "ports": [
            {"name": "mmio_addr", "direction": "input", "width": 8},
            {"name": "mmio_wdata", "direction": "input", "width": 32},
            {"name": "mmio_write", "direction": "input", "width": 1},
            {"name": "done", "direction": "output", "width": 1},
        ],
        "register_contract": {"registers": []},
        "feature_contracts": [{
            "id": "unknown", "description": "Unknown internal control.",
            "stimulus": {"cfg_missing": 1}, "expected": {"done": 1},
        }],
    }

    spec_agent._project_internal_feature_stimulus_to_register_bus(spec, "flat")

    compiled = compile_feature_contracts(spec, spec["ports"])
    assert compiled[0]["executable"] is False
    assert compiled[0]["unresolved_bindings"] == ["cfg_missing"]


def test_generation_prompt_renders_multicycle_json_example(tmp_path, monkeypatch):
    prompts = []

    def stop_after_prompt(prompt, agent_name, state, phase):
        prompts.append(prompt)
        raise RuntimeError("intentional prompt capture")

    monkeypatch.setattr(spec_agent, "_complete_spec_generation", stop_after_prompt)
    monkeypatch.setattr(spec_agent, "_upload_spec_debug_artifacts", lambda *args, **kwargs: None)

    result = spec_agent.run_agent({
        "workflow_id": "prompt-render-test",
        "workflow_dir": str(tmp_path),
        "spec": "Create a counter with enable and observable count output.",
        "top_module": "counter_top",
    })

    assert prompts
    assert '"stimulus":{"steps":[{"signals":{"port":value},"cycles":1}]}' in prompts[0]
    assert "Do not assume an output becomes zero merely because enable/request/write is inactive" in prompts[0]
    assert "Keep expected maps feature-focused" in prompts[0]
    assert "intentional prompt capture" in result["status"]


def _module(name: str):
    return {
        "name": name,
        "ports": [],
        "functionality": "Test module.",
        "responsibilities": [],
        "must_drive": [],
        "must_receive": [],
        "must_not_drive": [],
        "reset_behavior": "",
        "behavior_rules": [],
    }


def _port(name: str, direction: str, width: int = 1):
    return {"name": name, "direction": direction, "width": width}


def test_normalize_flat_spec_derives_missing_rtl_output_file():
    spec = {
        **_module("pwm_controller"),
        "description": "PWM controller.",
    }

    out, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "flat"
    assert out["rtl_output_file"] == "pwm_controller.v"


def test_normalize_flat_design_name_alias_preserves_register_contract():
    spec = {
        "design_name": "safety_fault_watchdog",
        "design_summary": "Automotive safety watchdog.",
        "ports": [{"name": "clk", "direction": "input", "width": 1}],
        "functionality": "Supervise heartbeat and latch faults.",
        "rtl_output_file": "safety_fault_watchdog.v",
        "register_contract": {"bus_type": "custom", "registers": [{"name": "CONTROL"}]},
    }

    out, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "flat"
    assert out["name"] == "safety_fault_watchdog"
    assert out["description"] == "Automotive safety watchdog."
    assert out["register_contract"]["registers"][0]["name"] == "CONTROL"


def test_mandatory_firmware_control_plane_rejects_direct_configuration_pins():
    spec = {
        **_module("adaptive_aero_control_top"),
        "ports": [_port("clk", "input"), _port("cfg_cmd_min_a", "input", 12)],
        "register_contract": {},
    }
    with pytest.raises(ValueError, match="missing a concrete register_contract"):
        spec_agent._validate_mandatory_firmware_control_plane(
            spec, "flat", "FIRMWARE CONTROL-PLANE CONTRACT (mandatory)",
        )


def test_structured_firmware_requirement_does_not_depend_on_prompt_marker():
    spec = {
        **_module("adaptive_aero_control_top"),
        "ports": [_port("clk", "input"), _port("cfg_cmd_min", "input", 12)],
        "register_contract": {},
    }
    with pytest.raises(ValueError, match="missing a concrete register_contract"):
        spec_agent._validate_mandatory_firmware_control_plane(
            spec,
            "flat",
            "ordinary application specification",
            required=True,
        )


def test_contract_repair_prompt_teaches_coherent_firmware_interface_repair():
    prompt = spec_agent._build_repair_prompt(
        "base contract",
        '{"register_contract": {}}',
        "Mandatory firmware control-plane contract is missing a concrete register_contract bus and registers",
    )

    assert "FIRMWARE CONTROL-PLANE REPAIR EXAMPLES" in prompt
    assert "GOOD:" in prompt
    assert "BAD:" in prompt
    assert "do not patch only the validation message" in prompt


def test_contract_repair_prompt_preserves_late_register_contract_from_large_json():
    previous = {
        "design_name": "large_design",
        "hierarchy": {"top_module": {"name": "large_top"}, "modules": []},
        "large_early_section": "x" * 20000,
        "register_contract": {
            "bus_type": "csr",
            "registers": [{"name": "CONTROL", "offset": "0x00", "access": "RW"}],
        },
    }

    prompt = spec_agent._build_repair_prompt(
        "base",
        json.dumps(previous, indent=2),
        "Mandatory firmware control-plane contract is missing a concrete register_contract bus and registers",
    )

    assert '"register_contract"' in prompt
    assert '"CONTROL"' in prompt


def test_contract_repair_prompt_teaches_complete_generic_connectivity_closure():
    prompt = spec_agent._build_repair_prompt(
        "base",
        "{}",
        "Required child input 'consumer.status_valid_in' has no source in "
        "top_level_connections or inter_module_signals. Other required child inputs "
        "without sources: 'monitor.status_valid'.",
    )

    assert "HIERARCHICAL CONNECTIVITY-CLOSURE REPAIR EXAMPLES" in prompt
    assert "producer status_valid_out and consumer status_valid_in" in prompt
    assert "repair one listed endpoint" in prompt
    assert "exactly one semantically valid structural source" in prompt


def test_hierarchical_contract_rejects_children_nested_inside_top_module():
    child = {
        **_module("child"),
        "ports": [_port("clk", "input")],
        "rtl_output_file": "child.v",
    }
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"),
                "ports": [_port("clk", "input")],
                "rtl_output_file": "top.v",
                "submodules": [child],
            },
            "modules": [],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["top.clk"]}],
        "inter_module_signals": [],
        "signal_ownership": [{"signal": "clk", "owner": "top.clk"}],
    }

    with pytest.raises(ValueError, match="hierarchy.modules.*top_module.submodules"):
        spec_agent._validate_spec_contract(spec, "hierarchical")

    prompt = spec_agent._build_repair_prompt("base", "{}", "Child module definitions must be declared in hierarchy.modules, not hierarchy.top_module.submodules")
    assert "HIERARCHY DELIVERABLE REPAIR EXAMPLES" in prompt
    assert "every instantiated child" in prompt


def test_fpga_memory_contract_rejects_openram_hard_macro():
    spec = {
        "memory_macros": [{"name": "staging_ram", "kind": "openram_sram"}],
    }

    with pytest.raises(ValueError, match="FPGA-only.*openram_sram"):
        spec_agent._validate_fpga_memory_contract(spec, "FPGA MEMORY CONTRACT (mandatory)")

    prompt = spec_agent._build_repair_prompt("base", "{}", "FPGA memory contract is FPGA-only and cannot use openram_sram")
    assert "FPGA MEMORY REPAIR EXAMPLES" in prompt
    assert "technology-neutral wrapper" in prompt


@pytest.mark.parametrize("kind", [
    "prebuilt_sram_macro", "precompiled-sram-macro", "asic_hard_memory", "sky130_sram_1kbyte",
])
def test_fpga_memory_kind_aliases_share_one_hard_macro_classifier(kind):
    assert spec_agent._is_fpga_forbidden_memory_kind(kind) is True
    spec = {"memory_macros": [{"name": "ram", "kind": kind}]}
    with pytest.raises(ValueError, match="FPGA-only"):
        spec_agent._validate_fpga_memory_contract(spec, "FPGA MEMORY CONTRACT (mandatory)")

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )
    assert normalized["memory_macros"][0]["kind"] == "fpga_bram"
    spec_agent._validate_fpga_memory_contract(
        normalized, "FPGA MEMORY CONTRACT (mandatory)",
    )


def test_fpga_terminal_closure_normalizes_hard_macro_without_changing_geometry():
    spec = {
        "memory_macros": [{
            "name": "history_store", "kind": "openram_sram", "depth": 256,
            "data_width": 128, "addr_width": 8, "instance_name": "u_history",
            "ports": {"clk": "clk", "we": "we", "addr": "addr", "din": "din", "dout": "dout"},
        }],
        "hierarchy": {"top_module": _module("top"), "modules": []},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    macro = normalized["memory_macros"][0]
    assert macro["kind"] == "fpga_bram"
    assert macro["depth"] == 256
    assert macro["data_width"] == 128
    assert macro["instance_name"] == "u_history"
    wrapper = normalized["hierarchy"]["modules"][0]
    assert wrapper["name"] == "history_store"
    assert wrapper["rtl_output_file"] == "history_store.v"
    spec_agent._validate_fpga_memory_contract(
        normalized, "FPGA MEMORY CONTRACT (mandatory)",
    )


def test_fpga_memory_name_collision_keeps_functional_wrapper_ports_authoritative():
    wrapper = {
        **_module("history_bram_if"),
        "ports": [
            _port("clk", "input"), _port("rst_n", "input"),
            _port("wr_en", "input"), _port("wr_addr", "input", 6),
            _port("wr_data", "input", 64), _port("rd_en", "input"),
            _port("rd_addr", "input", 6), _port("rd_data", "output", 64),
        ],
        "rtl_output_file": "history_bram_if.v",
    }
    spec = {
        "memory_macros": [{
            "name": "history_bram_if", "kind": "openram_sram", "depth": 64,
            "data_width": 64, "addr_width": 6, "instance_name": "u_history",
            "ports": {"clk": "clk", "csb": "csb", "we": "we", "addr": "addr", "din": "din", "dout": "dout"},
        }],
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper]},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert normalized["memory_macros"] == []
    kept = normalized["hierarchy"]["modules"][0]
    assert [port["name"] for port in kept["ports"]] == [
        "clk", "rst_n", "wr_en", "wr_addr", "wr_data", "rd_en", "rd_addr", "rd_data",
    ]
    assert kept["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 64, "data_width": 64,
        "addr_width": 6, "technology_binding": "technology_neutral_inferred_memory",
    }


def test_fpga_memory_collapses_differently_named_hard_macro_into_explicit_wrapper():
    wrapper = {
        **_module("application_history_wrapper"),
        "description": "Technology-neutral wrapper around the declared physical memory macro.",
        "functionality": "Preserve the underlying macro interface for history storage.",
        "ports": [
            _port("clk", "input"), _port("write_en", "input"),
            _port("write_addr", "input", 8), _port("write_data", "input", 64),
            _port("read_en", "input"), _port("read_addr", "input", 8),
            _port("read_data", "output", 64),
        ],
        "rtl_output_file": "application_history_wrapper.v",
    }
    spec = {
        "memory_macros": [{
            "name": "openram_sram_64x32", "kind": "openram_sram", "depth": 64,
            "data_width": 32, "addr_width": 6, "instance_name": "u_sram",
            "ports": {"clk": "clk", "csb": "csb", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
        }],
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper]},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert normalized["memory_macros"] == []
    assert [module["name"] for module in normalized["hierarchy"]["modules"]] == [
        "application_history_wrapper",
    ]
    assert wrapper["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 256, "data_width": 64,
        "addr_width": 8, "technology_binding": "technology_neutral_inferred_memory",
    }


def test_fpga_memory_recognizes_fifo_style_wrapper_over_hard_macro():
    wrapper = {
        **_module("payload_store"),
        "description": "Technology-neutral FIFO wrapper around the underlying SRAM macro.",
        "ports": [
            _port("write_en", "input"), _port("read_en", "input"),
            _port("push_data", "input", 64), _port("pop_data", "output", 64),
            _port("full", "output"), _port("empty", "output"),
        ],
    }
    spec = {
        "memory_macros": [{
            "name": "vendor_ram", "kind": "prebuilt_sram_macro",
            "depth": 256, "data_width": 64, "addr_width": 8,
        }],
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper]},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert normalized["memory_macros"] == []
    assert wrapper["memory_implementation"] == {
        "kind": "fpga_bram", "depth": 256, "data_width": 64,
        "addr_width": 8, "technology_binding": "technology_neutral_inferred_memory",
    }


def test_one_explicit_wrapper_can_own_multiple_inferred_memory_banks():
    wrapper = {
        **_module("payload_store"),
        "description": "Technology-neutral wrapper around the declared physical SRAM macros.",
        "ports": [
            _port("history_write_en", "input"), _port("history_read_en", "input"),
            _port("history_push_data", "input", 32), _port("history_pop_data", "output", 32),
            _port("feature_write_en", "input"), _port("feature_read_en", "input"),
            _port("feature_push_data", "input", 64), _port("feature_pop_data", "output", 64),
        ],
    }
    spec = {
        "memory_macros": [
            {"name": "history_ram", "kind": "openram_sram", "depth": 64, "data_width": 32, "addr_width": 6},
            {"name": "feature_ram", "kind": "prebuilt_sram_macro", "depth": 256, "data_width": 64, "addr_width": 8},
        ],
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper]},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert normalized["memory_macros"] == []
    assert wrapper["memory_implementation"]["kind"] == "fpga_bram"
    assert wrapper["memory_implementation"]["data_width"] == 64
    assert [(bank["name"], bank["depth"], bank["data_width"]) for bank in wrapper["memory_banks"]] == [
        ("history_ram", 64, 32), ("feature_ram", 256, 64),
    ]


def test_fpga_memory_does_not_guess_between_multiple_explicit_wrappers():
    def wrapper(name):
        return {
            **_module(name),
            "description": "Wrapper around the declared physical memory macro.",
            "ports": [
                _port("addr", "input", 6), _port("write_data", "input", 32),
                _port("read_data", "output", 32),
            ],
        }

    spec = {
        "memory_macros": [{
            "name": "ram", "kind": "openram_sram", "depth": 64,
            "data_width": 32, "addr_width": 6,
        }],
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper("a"), wrapper("b")]},
    }

    normalized = spec_agent._normalize_fpga_memory_contract(
        spec, "FPGA MEMORY CONTRACT (mandatory)",
    )

    assert len(normalized["memory_macros"]) == 1
    assert normalized["memory_macros"][0]["kind"] == "fpga_bram"
    assert all("memory_implementation" not in module for module in normalized["hierarchy"]["modules"][:2])


def test_inferred_fpga_memory_read_output_must_remain_observable_after_macro_collapse():
    memory = {
        **_module("history_store"),
        "memory_implementation": {
            "kind": "fpga_bram", "depth": 64, "data_width": 32, "addr_width": 6,
        },
        "ports": [
            _port("addr", "input", 6), _port("write_data", "input", 32),
            _port("read_data", "output", 32),
        ],
    }
    spec = {
        "memory_macros": [],
        "hierarchy": {"top_module": _module("top"), "modules": [memory]},
        "inter_module_signals": [], "top_level_connections": [],
    }

    with pytest.raises(ValueError, match=r"history_store\.read_data is unconsumed"):
        spec_agent._validate_required_memory_observability(spec)

    consumer = {
        **_module("reader"),
        "ports": [_port("history_data", "input", 32)],
    }
    spec["hierarchy"]["modules"].append(consumer)
    spec["inter_module_signals"] = [{
        "name": "history_data", "width": 32, "source": "history_store.read_data",
        "destinations": ["reader.history_data"],
    }]
    spec_agent._validate_required_memory_observability(spec)


def test_mandatory_firmware_control_plane_accepts_concrete_custom_csr_bus():
    spec = {
        **_module("adaptive_aero_control_top"),
        "ports": [
            _port("cfg_addr", "input", 8), _port("cfg_wdata", "input", 32),
            _port("cfg_rdata", "output", 32), _port("cfg_valid", "input"),
            _port("cfg_write", "input"), _port("cfg_ready", "output"),
        ],
        "register_contract": {
            "bus_type": "custom",
            "registers": [{"name": "CONTROL", "offset": "0x00"}],
        },
    }
    spec_agent._validate_mandatory_firmware_control_plane(
        spec, "flat", "FIRMWARE CONTROL-PLANE CONTRACT (mandatory)",
    )


def test_mandatory_firmware_control_plane_accepts_csr_wen_ren_strobes():
    spec = {
        **_module("adaptive_aero_control_top"),
        "ports": [
            _port("csr_addr", "input", 8),
            _port("csr_wdata", "input", 32),
            _port("csr_rdata", "output", 32),
            _port("csr_wen", "input"),
            _port("csr_ren", "input"),
            _port("csr_ready", "output"),
        ],
        "register_contract": {
            "bus_type": "custom_csr",
            "registers": [{"name": "CONTROL", "offset": "0x00"}],
        },
    }

    spec_agent._validate_mandatory_firmware_control_plane(
        spec,
        "flat",
        "ordinary application specification",
        required=True,
    )


def test_mandatory_firmware_control_plane_accepts_direction_suffixed_csr_strobes():
    spec = {
        **_module("adaptive_aero_control_top"),
        "ports": [
            _port("csr_addr_i", "input", 8),
            _port("csr_wdata_i", "input", 32),
            _port("csr_rdata_o", "output", 32),
            _port("csr_valid_i", "input"),
            _port("csr_we_i", "input"),
            _port("csr_ready_o", "output"),
        ],
        "register_contract": {
            "bus_type": "csr/mmio",
            "registers": [{"name": "CONTROL", "offset": "0x00"}],
        },
    }

    spec_agent._validate_mandatory_firmware_control_plane(
        spec,
        "flat",
        "ordinary application specification",
        required=True,
    )


def test_mandatory_firmware_control_plane_accepts_standard_wishbone_names():
    spec = {
        **_module("soft_cpu_top"),
        "ports": [
            _port("wb_adr_i", "input", 32),
            _port("wb_dat_i", "input", 32),
            _port("wb_dat_o", "output", 32),
            _port("wb_we_i", "input"),
            _port("wb_cyc_i", "input"),
            _port("wb_stb_i", "input"),
            _port("wb_ack_o", "output"),
        ],
        "register_contract": {
            "bus_type": "wishbone",
            "registers": [{"name": "CONTROL", "offset": "0x00"}],
        },
    }

    spec_agent._validate_mandatory_firmware_control_plane(
        spec, "flat", "ordinary application specification", required=True,
    )


def test_mandatory_firmware_control_plane_accepts_bare_direction_suffixed_wishbone_strobes():
    spec = {
        **_module("soft_cpu_top"),
        "ports": [
            _port("adr_i", "input", 32), _port("dat_i", "input", 32),
            _port("dat_o", "output", 32), _port("we_i", "input"),
            _port("cyc_i", "input"), _port("stb_i", "input"),
            _port("ack_o", "output"),
        ],
        "register_contract": {
            "bus_type": "wishbone",
            "registers": [{"name": "CONTROL", "offset": "0x00"}],
        },
    }

    spec_agent._validate_mandatory_firmware_control_plane(
        spec, "flat", "ordinary application specification", required=True,
    )


def test_parse_llm_json_object_prefers_last_spec_shaped_object():
    text = (
        '{"design_name":"draft","hierarchy":{"top_module":{"name":"draft"}}}'
        '\n'
        '{"design_name":"final","hierarchy":{"top_module":{"name":"final"}},"top_level_connections":[{"top_port":"clk"}]}'
    )

    parsed = spec_agent._parse_llm_json_object(text)

    assert parsed["design_name"] == "final"
    assert parsed["top_level_connections"][0]["top_port"] == "clk"


def test_parse_llm_json_prefers_complete_outer_contract_over_nested_hierarchy_fragment():
    text = json.dumps({
        "design_name": "soft_cpu_top",
        "hierarchy": {
            "top_module": {**_module("soft_cpu_top"), "ports": [_port("clk", "input")]},
            "modules": [
                {**_module("cpu"), "ports": [_port("req", "output")]},
                {**_module("peripheral"), "ports": [_port("req", "input")]},
            ],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["soft_cpu_top.clk"]},
        ],
        "inter_module_signals": [{
            "name": "req",
            "width": 1,
            "source": "cpu.req",
            "destinations": ["peripheral.req"],
            "description": "CPU request.",
        }],
        "signal_ownership": [{"signal": "req", "owner": "cpu.req"}],
        "register_contract": {"bus_type": "wishbone", "registers": [{"name": "CTRL"}]},
    })

    parsed = spec_agent._parse_llm_json_object(text)

    assert parsed["design_name"] == "soft_cpu_top"
    assert len(parsed["inter_module_signals"]) == 1
    assert parsed["inter_module_signals"][0]["source"] == "cpu.req"
    assert parsed["register_contract"]["bus_type"] == "wishbone"


def test_parse_llm_json_preserves_nonempty_contract_when_duplicate_keys_end_empty():
    text = """{
      "design_name": "adaptive_aero_control_top",
      "hierarchy": {
        "top_module": {
          "name": "adaptive_aero_control_top",
          "ports": [{"name":"clk","direction":"input","width":1},{"name":"cmd","direction":"output","width":16}],
          "responsibilities": ["Drive bounded commands"],
          "ports": [],
          "responsibilities": []
        },
        "modules": []
      }
    }"""

    parsed = spec_agent._parse_llm_json_object(text)
    top = parsed["hierarchy"]["top_module"]

    assert [port["name"] for port in top["ports"]] == ["clk", "cmd"]
    assert top["responsibilities"] == ["Drive bounded commands"]


def test_normalize_hierarchical_spec_derives_missing_rtl_output_files():
    spec = {
        "design_name": "pwm_controller",
        "hierarchy": {
            "top_module": _module("pwm_controller"),
            "modules": [_module("pwm_core")],
        },
    }

    out, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "hierarchical"
    assert out["hierarchy"]["top_module"]["rtl_output_file"] == "pwm_controller.v"
    assert out["hierarchy"]["modules"][0]["rtl_output_file"] == "pwm_core.v"


def test_memory_wrapper_direction_normalization_restores_read_data_producer():
    wrapper = {
        **_module("fpga_bram_history_wrapper"),
        "description": "Technology-neutral BRAM memory wrapper.",
        "ports": [
            _port("clk", "input"),
            _port("csb", "output"),
            _port("web", "output"),
            _port("addr", "output", 7),
            _port("din", "output", 32),
            _port("dout", "input", 32),
        ],
    }
    spec = {
        "hierarchy": {"top_module": _module("top"), "modules": [wrapper]},
    }

    out = spec_agent._normalize_memory_wrapper_port_directions(spec, "hierarchical")
    directions = {port["name"]: port["direction"] for port in out["hierarchy"]["modules"][0]["ports"]}

    assert directions == {
        "clk": "input", "csb": "input", "web": "input",
        "addr": "input", "din": "input", "dout": "output",
    }
    assert out["hierarchy"]["modules"][0]["must_drive"] == ["dout"]


def test_normalize_hierarchical_spec_uses_root_rtl_output_file_for_top():
    spec = {
        "design_name": "pwm_controller",
        "rtl_output_file": "custom_top.sv",
        "hierarchy": {
            "top_module": _module("pwm_controller"),
            "modules": [_module("pwm_core")],
        },
    }

    out, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "hierarchical"
    assert out["hierarchy"]["top_module"]["rtl_output_file"] == "custom_top.sv"


def test_normalize_hierarchical_spec_removes_duplicate_top_child():
    duplicate_top = {
        **_module("pwm_controller"),
        "rtl_output_file": "pwm_controller.v",
        "description": "Duplicate top emitted as a child.",
    }
    spec = {
        "design_name": "pwm_controller",
        "hierarchy": {
            "top_module": _module("pwm_controller"),
            "modules": [duplicate_top, _module("pwm_core")],
        },
    }

    out, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "hierarchical"
    assert out["hierarchy"]["top_module"]["description"] == "Duplicate top emitted as a child."
    assert [m["name"] for m in out["hierarchy"]["modules"]] == ["pwm_core"]


def test_hierarchical_validation_allows_top_internal_interconnect_nets():
    top = {
        **_module("controller"),
        "ports": [_port("clk", "input"), _port("reset_n", "input")],
        "rtl_output_file": "controller.v",
    }
    child = {
        **_module("sram_model"),
        "ports": [_port("clk", "input"), _port("csb", "input")],
        "rtl_output_file": "sram_model.v",
    }
    spec = {
        "design_name": "controller",
        "hierarchy": {"top_module": top, "modules": [child]},
        "top_level_connections": [{"top_port": "clk", "connected_to": ["sram_model.clk"]}],
        "inter_module_signals": [
            {"name": "mem_csb", "width": 1, "source": "controller.mem_csb", "destinations": ["sram_model.csb"]}
        ],
        "signal_ownership": [{"signal": "mem_csb", "owner": "controller.mem_csb"}],
    }

    spec_agent._validate_spec_contract(spec, "hierarchical")


def test_normalize_derives_only_unique_child_to_child_inter_module_signals():
    spec = {
        "design_name": "controller",
        "hierarchy": {
            "top_module": {
                **_module("controller"),
                "functionality": "Controller instantiates sram_wrapper.",
                "ports": [_port("clk", "input"), _port("csb", "input"), _port("rd_data", "output", 32)],
                "rtl_output_file": "controller.v",
            },
            "modules": [
                {
                    **_module("sram_wrapper"),
                    "ports": [
                        _port("clk", "input"),
                        _port("csb", "input"),
                        _port("addr", "input", 8),
                        _port("dout", "output", 32),
                    ],
                    "rtl_output_file": "sram_wrapper.v",
                },
                {
                    **_module("fallback_model"),
                    "ports": [_port("clk", "input"), _port("addr", "output", 8)],
                    "rtl_output_file": "fallback_model.v",
                }
            ],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["sram_wrapper.clk", "fallback_model.clk"]},
            {"top_port": "csb", "connected_to": ["sram_wrapper.csb"]},
            {"top_port": "rd_data", "connected_to": ["sram_wrapper.dout"]},
        ],
        "inter_module_signals": [],
        "signal_ownership": [],
    }

    out, mode = spec_agent._normalize_spec_json(spec)
    out = spec_agent._ensure_hierarchical_top_level_connections(out)
    out = spec_agent._ensure_hierarchical_inter_module_signals(out)
    out = spec_agent._ensure_hierarchical_port_closure(out)
    out = spec_agent._reconcile_hierarchical_signal_directions(out, mode)
    out = spec_agent._sanitize_hierarchical_connectivity(out)

    names = {sig["name"] for sig in out["inter_module_signals"]}
    assert "fallback_model_addr" in names
    assert "sram_wrapper_csb" not in names
    assert all(sig["name"] != "sram_wrapper_clk" for sig in out["inter_module_signals"])
    assert all(sig["name"] != "sram_wrapper_dout" for sig in out["inter_module_signals"])
    assert all(not endpoint.startswith("controller.") for sig in out["inter_module_signals"] for endpoint in [sig["source"], *sig["destinations"]])
    spec_agent._validate_spec_contract(out, mode)


def test_partial_inter_module_graph_is_completed_and_orphans_are_rejected():
    spec = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {
                **_module("top"),
                "ports": [_port("clk", "input")],
                "rtl_output_file": "top.v",
            },
            "modules": [
                {
                    **_module("producer"),
                    "ports": [_port("clk", "input"), _port("data", "output", 8), _port("valid", "output")],
                    "rtl_output_file": "producer.v",
                },
                {
                    **_module("consumer"),
                    "ports": [_port("clk", "input"), _port("data", "input", 8), _port("valid", "input"), _port("orphan", "input")],
                    "rtl_output_file": "consumer.v",
                },
            ],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["producer.clk", "consumer.clk"]},
        ],
        "inter_module_signals": [
            {"name": "valid", "width": 1, "source": "producer.valid", "destinations": ["consumer.valid"]},
        ],
        "signal_ownership": [{"signal": "valid", "owner": "producer.valid"}],
    }

    out = spec_agent._ensure_hierarchical_inter_module_signals(spec)
    edges = {
        (signal["source"], destination)
        for signal in out["inter_module_signals"]
        for destination in signal["destinations"]
    }
    assert ("producer.valid", "consumer.valid") in edges
    assert ("producer.data", "consumer.data") in edges

    with pytest.raises(ValueError, match="consumer.orphan.*has no source"):
        spec_agent._validate_spec_contract(out, "hierarchical")


def test_contract_backed_connection_materializes_omitted_ports_before_sanitizing():
    producer = {
        **_module("producer"),
        "ports": [_port("clk", "input")],
        "must_drive": ["history_data"],
        "rtl_output_file": "producer.v",
    }
    consumer = {
        **_module("consumer"),
        "ports": [_port("clk", "input")],
        "must_receive": ["history_data"],
        "rtl_output_file": "consumer.v",
    }
    spec = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [_port("clk", "input")]},
            "modules": [producer, consumer],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["producer.clk", "consumer.clk"]},
        ],
        "inter_module_signals": [{
            "name": "history_data",
            "width": 64,
            "source": "producer.history_data",
            "destinations": ["consumer.history_data"],
        }],
        "signal_ownership": [{"signal": "history_data", "owner": "producer.history_data"}],
    }

    out = spec_agent._materialize_contract_backed_connection_ports(spec)
    producer_port = next(port for port in producer["ports"] if port["name"] == "history_data")
    consumer_port = next(port for port in consumer["ports"] if port["name"] == "history_data")

    assert producer_port == {"name": "history_data", "direction": "output", "width": 64}
    assert consumer_port == {"name": "history_data", "direction": "input", "width": 64}
    assert spec_agent._sanitize_hierarchical_connectivity(out)["inter_module_signals"]


def test_contract_backed_connection_does_not_trust_unowned_or_undeclared_endpoints():
    producer = {**_module("producer"), "ports": [], "must_drive": ["data"]}
    consumer = {**_module("consumer"), "ports": [], "must_receive": []}
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "ports": []},
            "modules": [producer, consumer],
        },
        "inter_module_signals": [{
            "name": "data", "width": 8, "source": "producer.data",
            "destinations": ["consumer.data"],
        }],
        "signal_ownership": [],
    }

    spec_agent._materialize_contract_backed_connection_ports(spec)

    assert producer["ports"] == []
    assert consumer["ports"] == []


def test_connectivity_diagnostics_explain_unwired_ownership_width_mismatch():
    spec = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {**_module("top"), "ports": []},
            "modules": [
                {**_module("producer"), "ports": [_port("cmd", "output", 16)]},
                {**_module("history"), "ports": [_port("write_data", "input", 64)]},
            ],
        },
        "top_level_connections": [],
        "inter_module_signals": [],
        "signal_ownership": [{"signal": "history_write_data", "owner": "producer.cmd"}],
    }

    diagnostics = spec_agent._build_connectivity_repair_diagnostics(json.dumps(spec))

    assert "UNDRIVEN history.write_data" in diagnostics
    assert "owner width 16 does not match consumer width 64" in diagnostics
    assert "Ownership metadata is not a wire" in diagnostics


def test_connectivity_diagnostics_list_semantic_candidates_for_orphan_inputs():
    spec = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {**_module("top"), "ports": []},
            "modules": [
                {**_module("supervisor"), "ports": [_port("link_health", "output", 4)]},
                {**_module("regfile"), "ports": [_port("cfg_link_health", "input", 4)]},
            ],
        },
        "top_level_connections": [], "inter_module_signals": [], "signal_ownership": [],
    }

    diagnostics = spec_agent._build_connectivity_repair_diagnostics(json.dumps(spec))

    assert "UNDRIVEN regfile.cfg_link_health (width 4)" in diagnostics
    assert "supervisor.link_health (width 4, compatible)" in diagnostics


def test_invalid_self_ownership_does_not_hide_real_cross_module_candidate():
    spec = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {**_module("top"), "ports": []},
            "modules": [
                {**_module("supervisor"), "ports": [_port("link_health", "output", 4)]},
                {**_module("packer"), "ports": [
                    _port("link_health", "input", 4), _port("req_link_health", "output", 4),
                ]},
            ],
        },
        "top_level_connections": [], "inter_module_signals": [],
        "signal_ownership": [{"signal": "link_health", "owner": "packer.req_link_health"}],
    }

    diagnostics = spec_agent._build_connectivity_repair_diagnostics(json.dumps(spec))

    assert "would create feedback" in diagnostics
    assert "supervisor.link_health (width 4, compatible)" in diagnostics


def test_orphan_endpoint_count_supports_repair_regression_guard():
    error = (
        "Required child input 'regfile.fault_status' has no source. "
        "Other required child inputs without sources: 'history.addr', 'regfile.fault_status'."
    )

    assert spec_agent._orphan_endpoint_count(error) == 2
    assert spec_agent._orphan_endpoint_count("Feature contract is invalid") is None


def test_connectivity_repair_prompt_prevents_orphan_migration():
    prompt = spec_agent._build_repair_prompt(
        base_prompt="Generate a hierarchy.",
        previous_json_text='{"design_name":"top"}',
        failure_log_text="Required child input 'fifo.wr_en' has no source",
        strict_connectivity=True,
    )

    assert "Do not add any new child input ports" in prompt
    assert "remove that entire module" in prompt
    assert "without connecting it in the same response" in prompt
    assert "state computed by the consumer module itself" in prompt
    assert "Internal state is not an external consumer" in prompt
    assert "STRICT PASS3/PASS4 CONNECTIVITY REPAIR" in prompt
    assert "producer.payload_out" in prompt
    assert "Inputs are consumers" in prompt
    assert "memory read-data input" in prompt
    assert "memory wrapper's dout is the producer" in prompt
    assert "CSR/MMIO register block produce an explicit write/accept pulse" in prompt
    assert "response write/commit event" in prompt
    assert "creates feedback" in prompt


def test_pass5_graph_closure_prompt_requires_a_concrete_new_repair():
    ordinary = spec_agent._build_repair_prompt(
        "base", "{}", "Required child input 'cpu.imem_dout' has no source",
        strict_connectivity=True,
    )
    final = spec_agent._build_repair_prompt(
        "base", "{}", "Required child input 'cpu.imem_dout' has no source",
        strict_connectivity=True,
        final_graph_closure=True,
    )

    assert "FINAL GRAPH-CLOSURE PASS" not in ordinary
    assert "FINAL GRAPH-CLOSURE PASS" in final
    assert "Do not return the previous JSON unchanged" in final
    assert "authoritative checklist" in final
    assert "complete previous JSON below is the authoritative design" in final
    assert "combined fault needs an explicit aggregator output" in final
    assert "base" not in final
    assert ordinary != final


def test_removes_undriven_self_owned_alias_input_without_signal_name_rules():
    module = {
        **_module("controller"),
        "ports": [
            _port("trigger_state", "input"),
            _port("trigger_status", "output"),
        ],
        "must_receive": ["trigger_state"],
        "must_not_drive": ["trigger_state"],
        "must_drive": ["trigger_status"],
    }
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [], "rtl_output_file": "top.v"},
            "modules": [module],
        },
        "top_level_connections": [],
        "inter_module_signals": [],
        "signal_ownership": [{"signal": "trigger_state", "owner": "controller.trigger_status"}],
    }

    out = spec_agent._remove_self_owned_alias_inputs(spec)

    assert [port["name"] for port in out["hierarchy"]["modules"][0]["ports"]] == ["trigger_status"]
    assert out["hierarchy"]["modules"][0]["must_receive"] == []
    assert out["hierarchy"]["modules"][0]["must_not_drive"] == []


def test_preserves_self_owned_alias_input_when_it_has_real_external_source():
    module = {
        **_module("controller"),
        "ports": [_port("trigger_state", "input"), _port("trigger_status", "output")],
    }
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [_port("trigger", "input")], "rtl_output_file": "top.v"},
            "modules": [module],
        },
        "top_level_connections": [{"top_port": "trigger", "connected_to": ["controller.trigger_state"]}],
        "inter_module_signals": [],
        "signal_ownership": [{"signal": "trigger_state", "owner": "controller.trigger_status"}],
    }

    out = spec_agent._remove_self_owned_alias_inputs(spec)

    assert {port["name"] for port in out["hierarchy"]["modules"][0]["ports"]} == {"trigger_state", "trigger_status"}


def test_connectivity_repair_prompt_explains_rejected_graph_edges():
    previous = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [], "rtl_output_file": "top.v"},
            "modules": [
                {**_module("status"), "ports": [_port("pending", "input"), _port("age", "output", 32)], "rtl_output_file": "status.v"},
                {**_module("safety"), "ports": [_port("fault", "input")], "rtl_output_file": "safety.v"},
            ],
        },
        "top_level_connections": [],
        "inter_module_signals": [
            {"name": "feedback", "width": 1, "source": "status.pending", "destinations": ["safety.fault"]},
            {"name": "bad_width", "width": 32, "source": "status.age", "destinations": ["safety.fault"]},
        ],
        "signal_ownership": [],
    }

    prompt = spec_agent._build_repair_prompt(
        "base",
        json.dumps(previous),
        "Required child input 'safety.fault' has no source",
        strict_connectivity=True,
        final_graph_closure=True,
    )

    assert "STRUCTURAL GRAPH DIAGNOSTICS FROM THE PREVIOUS JSON" in prompt
    assert "status.pending -> safety.fault" in prompt
    assert "source is a input consumer port" in prompt
    assert "destination width 1 does not match signal width 32" in prompt


def test_sanitize_connectivity_keeps_one_width_compatible_producer_per_input():
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [_port("clk", "input")], "rtl_output_file": "top.v"},
            "modules": [
                {**_module("packager"), "ports": [_port("capture_request", "output"), _port("descriptor_valid", "output"), _port("descriptor", "output", 32)], "rtl_output_file": "packager.v"},
                {**_module("validator"), "ports": [_port("validated_command", "output", 16)], "rtl_output_file": "validator.v"},
                {**_module("supervisor"), "ports": [_port("request_captured", "input"), _port("validated_command", "input", 16)], "rtl_output_file": "supervisor.v"},
            ],
        },
        "top_level_connections": [],
        "inter_module_signals": [
            {"name": "request_captured", "width": 1, "source": "packager.capture_request", "destinations": ["supervisor.request_captured"]},
            {"name": "request_descriptor_valid", "width": 1, "source": "packager.descriptor_valid", "destinations": ["supervisor.request_captured"]},
            {"name": "request_descriptor", "width": 32, "source": "packager.descriptor", "destinations": ["supervisor.validated_command"]},
            {"name": "validated_command", "width": 16, "source": "validator.validated_command", "destinations": ["supervisor.validated_command"]},
        ],
        "signal_ownership": [],
    }

    out = spec_agent._sanitize_hierarchical_connectivity(spec)

    edges = {(sig["source"], destination) for sig in out["inter_module_signals"] for destination in sig["destinations"]}
    assert edges == {
        ("packager.capture_request", "supervisor.request_captured"),
        ("validator.validated_command", "supervisor.validated_command"),
    }


def test_sanitize_connectivity_rejects_top_input_as_derived_signal_owner():
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"),
                "ports": [_port("cfg_wdata", "input", 32), _port("req_ready", "input")],
                "rtl_output_file": "top.v",
            },
            "modules": [
                {
                    **_module("consumer"),
                    "ports": [_port("cfg_enable", "input"), _port("request_ready", "input")],
                    "rtl_output_file": "consumer.v",
                }
            ],
        },
        "top_level_connections": [],
        "inter_module_signals": [
            {"name": "cfg_enable", "width": 1, "source": "top.cfg_wdata", "destinations": ["consumer.cfg_enable"]},
            {"name": "request_ready", "width": 1, "source": "top.req_ready", "destinations": ["consumer.request_ready"]},
        ],
        "signal_ownership": [
            {"signal": "cfg_enable", "owner": "top.cfg_wdata"},
            {"signal": "request_ready", "owner": "top.req_ready"},
        ],
    }

    out = spec_agent._sanitize_hierarchical_connectivity(spec)

    assert out["inter_module_signals"] == []
    assert out["signal_ownership"] == []


def test_normalize_adds_referenced_memory_macro_module():
    spec = {
        "design_name": "controller",
        "memory_macros": [
            {
                "name": "demo_sram_32x64_model",
                "kind": "synthesizable_memory_model",
                "data_width": 32,
                "addr_width": 6,
                "ports": {"clk": "clk", "csb": "csb", "web": "web", "addr": "addr", "din": "din", "dout": "dout"},
            }
        ],
        "hierarchy": {
            "top_module": {
                **_module("controller"),
                "ports": [_port("clk", "input")],
                "rtl_output_file": "controller.v",
            },
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["controller.clk"]}],
        "inter_module_signals": [
            {"name": "mem_csb", "width": 1, "source": "controller.mem_csb", "destinations": ["demo_sram_32x64_model.csb"]},
            {"name": "mem_dout", "width": 32, "source": "demo_sram_32x64_model.dout", "destinations": ["controller.mem_dout"]},
        ],
        "signal_ownership": [
            {"signal": "mem_csb", "owner": "controller.mem_csb"},
            {"signal": "mem_dout", "owner": "demo_sram_32x64_model.dout"},
        ],
    }

    out, mode = spec_agent._normalize_spec_json(spec)
    out = spec_agent._ensure_hierarchical_top_level_connections(out)
    spec_agent._validate_spec_contract(out, mode)

    memory_module = out["hierarchy"]["modules"][0]
    assert memory_module["name"] == "demo_sram_32x64_model"
    assert memory_module["rtl_output_file"] == "demo_sram_32x64_model.v"
    assert {p["name"]: p["width"] for p in memory_module["ports"]}["addr"] == 6
    assert {p["name"]: p["width"] for p in memory_module["ports"]}["din"] == 32


def test_normalize_materializes_declared_memory_macro_even_before_wiring():
    spec = {
        "design_name": "controller",
        "memory_macros": [{
            "name": "payload_bram", "kind": "fpga_bram", "data_width": 64, "addr_width": 5,
            "ports": {"clk": "clk", "csb": "csb", "we": "we", "addr": "addr", "din": "din", "dout": "dout"},
        }],
        "hierarchy": {
            "top_module": {**_module("controller"), "ports": [_port("clk", "input")], "rtl_output_file": "controller.v"},
            "modules": [],
        },
        "top_level_connections": [], "inter_module_signals": [], "signal_ownership": [],
    }

    normalized, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "hierarchical"
    assert [module["name"] for module in normalized["hierarchy"]["modules"]] == ["payload_bram"]


def test_json_syntax_repair_prompt_preserves_large_contract_middle():
    marker = "MIDDLE_FEATURE_CONTRACT_MUST_SURVIVE"
    previous = "A" * 26000 + marker + "B" * 26000

    prompt = spec_agent._build_json_syntax_repair_prompt(previous, "missing final brace")

    assert marker in prompt


def test_fpga_memory_contract_rejects_external_data_control_edges_but_allows_clock():
    spec = {
        "memory_macros": [{
            "name": "payload_bram", "kind": "fpga_bram",
            "ports": {"clk": "clk", "csb": "csb", "addr": "addr", "din": "din", "dout": "dout"},
        }],
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["payload_bram.clk"]},
            {"top_port": "mem_addr", "connected_to": ["payload_bram.addr"]},
        ],
    }

    with pytest.raises(ValueError, match="connectivity to remain internal.*mem_addr->payload_bram.addr"):
        spec_agent._validate_fpga_memory_contract(spec, "FPGA MEMORY CONTRACT (mandatory)")

    spec["top_level_connections"] = [{"top_port": "clk", "connected_to": ["payload_bram.clk"]}]
    spec_agent._validate_fpga_memory_contract(spec, "FPGA MEMORY CONTRACT (mandatory)")


def test_extract_memory_macros_from_prompt_contract_lines():
    prompt = """
Structured memory macro contract:
- memory_macros[0].name = sky130_sram_1kbyte_1rw1r_32x256_8
- memory_macros[0].kind = prebuilt_sky130_sram
- memory_macros[0].depth = 256
- memory_macros[0].data_width = 32
- memory_macros[0].addr_width = 8
- memory_macros[0].instance_name = u_sram
- memory_macros[0].requires_mbist = true
- memory_macros[0].ports.clk = clk
- memory_macros[0].ports.csb = csb
- memory_macros[0].ports.we = web
- memory_macros[0].ports.addr = addr
- memory_macros[0].ports.din = din
- memory_macros[0].ports.dout = dout
"""

    macros = spec_agent._extract_memory_macros_from_prompt(prompt)

    assert macros == [
        {
            "name": "sky130_sram_1kbyte_1rw1r_32x256_8",
            "kind": "prebuilt_sky130_sram",
            "depth": 256,
            "data_width": 32,
            "addr_width": 8,
            "instance_name": "u_sram",
            "requires_mbist": True,
            "ports": {
                "clk": "clk",
                "csb": "csb",
                "we": "web",
                "addr": "addr",
                "din": "din",
                "dout": "dout",
            },
        }
    ]


def test_extract_top_ports_from_prompt_sections():
    prompt = """
Top module:
- sram_mbist_demo_controller

Inputs:
- clk
- reset_n
- wr_addr[7:0]
- wr_data[31:0]

Outputs:
- rd_data[31:0]
- ready

Memory intent:
- Use SRAM.
"""

    ports = spec_agent._extract_top_ports_from_prompt(prompt)

    assert ports == [
        {"name": "clk", "direction": "input", "width": 1},
        {"name": "reset_n", "direction": "input", "width": 1, "active_low": True},
        {"name": "wr_addr", "direction": "input", "width": 8},
        {"name": "wr_data", "direction": "input", "width": 32},
        {"name": "rd_data", "direction": "output", "width": 32},
        {"name": "ready", "direction": "output", "width": 1},
    ]


def test_explicit_prompt_memory_macro_overrides_model_fallback_identity():
    generated = {
        "name": "controller",
        "memory_macros": [{"name": "demo_sram_32x256_model", "kind": "inferred"}],
    }
    prompt = """
memory_macros[0].name = sky130_sram_1kbyte_1rw1r_32x256_8
memory_macros[0].kind = prebuilt_sky130_sram
memory_macros[0].depth = 256
memory_macros[0].data_width = 32
memory_macros[0].addr_width = 8
memory_macros[0].instance_name = u_sram
memory_macros[0].requires_mbist = true
memory_macros[0].ports.clk = clk
memory_macros[0].ports.csb = csb
memory_macros[0].ports.we = web
memory_macros[0].ports.addr = addr
memory_macros[0].ports.din = din
memory_macros[0].ports.dout = dout
"""

    result = spec_agent._merge_prompt_memory_macros(generated, prompt)

    assert result["memory_macros"] == [{
        "name": "sky130_sram_1kbyte_1rw1r_32x256_8",
        "kind": "prebuilt_sky130_sram",
        "depth": 256,
        "data_width": 32,
        "addr_width": 8,
        "instance_name": "u_sram",
        "requires_mbist": True,
        "ports": {"clk": "clk", "csb": "csb", "we": "web", "addr": "addr", "din": "din", "dout": "dout"},
    }]


def test_compile_spec_contract_repairs_empty_flat_ports_from_prompt(tmp_path):
    llm_output = json.dumps(
        {
            "name": "sram_mbist_demo_controller",
            "description": "Controller.",
            "ports": [],
            "functionality": "",
            "responsibilities": [],
            "must_drive": [],
            "must_receive": [],
            "must_not_drive": [],
            "reset_behavior": "",
            "behavior_rules": [],
            "rtl_output_file": "sram_mbist_demo_controller.sv",
        }
    )
    prompt = """
Inputs:
- clk
- reset_n
- wr_en
- wr_addr[7:0]
- wr_data[31:0]
- rd_en
- rd_addr[7:0]
- bist_start

Outputs:
- rd_data[31:0]
- ready
- bist_done
- bist_fail
- irq
"""

    spec, mode, _, _ = spec_agent._compile_spec_contract(
        llm_output,
        str(tmp_path),
        requested_top="sram_mbist_demo_controller",
        source_prompt=prompt,
    )

    assert mode == "flat"
    assert [p["name"] for p in spec["ports"]] == [
        "clk",
        "reset_n",
        "wr_en",
        "wr_addr",
        "wr_data",
        "rd_en",
        "rd_addr",
        "bist_start",
        "rd_data",
        "ready",
        "bist_done",
        "bist_fail",
        "irq",
    ]
    assert spec["must_receive"] == ["clk", "reset_n", "wr_en", "wr_addr", "wr_data", "rd_en", "rd_addr", "bist_start"]
    assert spec["must_drive"] == ["rd_data", "ready", "bist_done", "bist_fail", "irq"]


def test_compile_spec_contract_filters_leaked_internal_sram_top_ports_from_prompt(tmp_path):
    llm_output = json.dumps(
        {
            "design_name": "sram_mbist_demo_controller",
            "hierarchy": {
                "top_module": {
                    "name": "sram_mbist_demo_controller",
                    "rtl_output_file": "sram_mbist_demo_controller.v",
                    "ports": [
                        {"name": "clk", "direction": "input", "width": 1},
                        {"name": "reset_n", "direction": "input", "width": 1},
                        {"name": "rd_data", "direction": "output", "width": 32},
                        {"name": "sram_csb", "direction": "output", "width": 1},
                        {"name": "sram_dout", "direction": "input", "width": 32},
                    ],
                },
                "modules": [],
            },
        }
    )
    prompt = """
Inputs:
- clk
- reset_n

Outputs:
- rd_data[31:0]
"""

    spec, mode, _, _ = spec_agent._compile_spec_contract(
        llm_output,
        str(tmp_path),
        requested_top="sram_mbist_demo_controller",
        source_prompt=prompt,
    )

    assert mode == "hierarchical"
    assert [p["name"] for p in spec["hierarchy"]["top_module"]["ports"]] == ["clk", "reset_n", "rd_data"]
    assert "sram_csb" not in spec["hierarchy"]["top_module"]["must_drive"]
    assert "sram_dout" not in spec["hierarchy"]["top_module"]["must_receive"]


def test_normalize_accepts_hierarchy_submodules_alias():
    spec = {
        "design_name": "demo",
        "hierarchy": {
            "top_module": {
                "name": "top",
                "rtl_output_file": "top.v",
                "ports": [{"name": "clk", "direction": "input", "width": 1}],
            },
            "submodules": [
                {
                    "name": "demo_sram_32x256_wrapper",
                    "ports": [
                        {"name": "clk", "direction": "input", "width": 1},
                        {"name": "dout", "direction": "output", "width": 32},
                    ],
                }
            ],
        },
    }

    norm, mode = spec_agent._normalize_spec_json(spec)

    assert mode == "hierarchical"
    assert [m["name"] for m in norm["hierarchy"]["modules"]] == ["demo_sram_32x256_wrapper"]
    assert norm["hierarchy"]["modules"][0]["rtl_output_file"] == "demo_sram_32x256_wrapper.v"


def test_parse_repairs_array_closed_as_object_before_next_key():
    malformed = (
        '{"design_name":"demo","hierarchy":{"top_module":{"name":"top","ports":[],'
        '"behavior_rules":["rule one","rule two"},"rtl_output_file":"top.v"},"modules":[]}'
    )

    parsed = spec_agent._parse_llm_json_object(malformed)

    assert parsed["hierarchy"]["top_module"]["behavior_rules"] == ["rule one", "rule two"]
    assert parsed["hierarchy"]["top_module"]["rtl_output_file"] == "top.v"


def test_parse_repairs_duplicated_unmatched_array_closer_before_next_key():
    malformed = (
        '{"name":"pwm_fpga_demo","ports":[{"name":"clk","direction":"input","width":1},'
        '{"name":"led","direction":"output","width":1}],'
        '"functionality":"PWM LED demo.","responsibilities":["Generate PWM."],'
        '"must_drive":["led"],"must_receive":["clk"],"must_not_drive":[],'
        '"reset_behavior":"No reset.","behavior_rules":["Single clock."]],'
        '"rtl_output_file":"pwm_fpga_demo.v"}'
    )

    parsed = spec_agent._parse_llm_json_object(malformed)

    assert parsed["name"] == "pwm_fpga_demo"
    assert parsed["behavior_rules"] == ["Single clock."]
    assert parsed["rtl_output_file"] == "pwm_fpga_demo.v"

def test_parse_repairs_array_bracket_drift_then_eof_truncation():
    malformed = (
        '{"design_name":"demo","hierarchy":{"top_module":{"name":"top","ports":[],'
        '"behavior_rules":["rule one","rule two"},"rtl_output_file":"top.v"},"modules":[]'
    )

    parsed = spec_agent._parse_llm_json_object(malformed)

    assert parsed["hierarchy"]["top_module"]["behavior_rules"] == ["rule one", "rule two"]
    assert parsed["hierarchy"]["top_module"]["rtl_output_file"] == "top.v"


def test_compile_spec_contract_recovers_prompt_memory_macros(tmp_path):
    llm_output = json.dumps(
        {
            "name": "sram_mbist_demo_controller",
            "description": "Controller.",
            "ports": [_port("clk", "input"), _port("ready", "output")],
            "functionality": "Controller.",
            "responsibilities": [],
            "must_drive": ["ready"],
            "must_receive": ["clk"],
            "must_not_drive": ["clk"],
            "reset_behavior": "",
            "behavior_rules": [],
        }
    )
    prompt = "- memory_macros[0].name = sky130_sram_1kbyte_1rw1r_32x256_8\n- memory_macros[0].depth = 256\n- memory_macros[0].data_width = 32\n- memory_macros[0].addr_width = 8\n- memory_macros[0].requires_mbist = true\n"

    spec, mode, _, _ = spec_agent._compile_spec_contract(
        llm_output,
        str(tmp_path),
        requested_top="sram_mbist_demo_controller",
        source_prompt=prompt,
    )

    assert mode == "flat"
    assert spec["memory_macros"][0]["name"] == "sky130_sram_1kbyte_1rw1r_32x256_8"
    assert spec["memory_macros"][0]["depth"] == 256
    assert spec["memory_macros"][0]["requires_mbist"] is True


def test_normalize_single_module_hierarchy_defaults_contract_and_drops_self_loops():
    spec = {
        "design_name": "sram_mbist_demo_controller",
        "register_map": [{"name": "CONTROL", "offset": "0x00"}],
        "hierarchy": {
            "top_module": {
                "name": "sram_mbist_demo_controller",
                "ports": [_port("clk", "input"), _port("ready", "output")],
                "must_drive": ["ready"],
                "must_receive": ["clk"],
                "functionality": "Controller.",
            }
        },
        "top_level_connections": [{"top_port": "ready", "connected_to": ["sram_mbist_demo_controller.ready"]}],
        "inter_module_signals": [
            {
                "name": "ready_sig",
                "width": 1,
                "source": "sram_mbist_demo_controller.ready",
                "destinations": ["sram_mbist_demo_controller.ready"],
            }
        ],
        "signal_ownership": [{"signal": "ready", "owner": "sram_mbist_demo_controller.ready"}],
    }

    out, mode = spec_agent._normalize_spec_json(spec)
    spec_agent._validate_spec_contract(out, mode)

    top = out["hierarchy"]["top_module"]
    assert top["rtl_output_file"] == "sram_mbist_demo_controller.v"
    assert top["responsibilities"] == []
    assert top["must_not_drive"] == []
    assert out["inter_module_signals"] == []
    assert out["register_contract"] == [{"name": "CONTROL", "offset": "0x00"}]
    assert out["top_level_connections"][0]["connected_to"] == ["sram_mbist_demo_controller.ready"]


def test_single_module_hierarchy_generates_top_self_connections_when_missing():
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("controller"),
                "ports": [_port("clk", "input"), _port("ready", "output")],
                "rtl_output_file": "controller.v",
            },
            "modules": [],
        },
        "top_level_connections": [],
    }

    out = spec_agent._ensure_hierarchical_top_level_connections(spec)

    assert out["top_level_connections"] == [
        {
            "top_port": "clk",
            "connected_to": ["controller.clk"],
            "description": "Top-level port clk connected to matching child module port(s).",
        },
        {
            "top_port": "ready",
            "connected_to": ["controller.ready"],
            "description": "Top-level port ready connected to matching child module port(s).",
        },
    ]


def test_partial_top_connections_are_completed_and_unique_reversed_ports_are_reconciled():
    child = {
        **_module("transport_adapter"),
        "ports": [
            _port("clk", "input"),
            _port("model_req_valid", "input"),
            _port("model_rsp_valid", "output"),
        ],
    }
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"),
                "ports": [
                    _port("clk", "input"),
                    _port("model_req_valid", "output"),
                    _port("model_rsp_valid", "input"),
                ],
            },
            "modules": [child],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["transport_adapter.clk"]}],
    }

    out = spec_agent._ensure_hierarchical_top_level_connections(spec)

    by_top = {item["top_port"]: item["connected_to"] for item in out["top_level_connections"]}
    assert by_top["clk"] == ["transport_adapter.clk"]
    assert by_top["model_req_valid"] == ["transport_adapter.model_req_valid"]
    assert by_top["model_rsp_valid"] == ["transport_adapter.model_rsp_valid"]
    directions = {port["name"]: port["direction"] for port in child["ports"]}
    assert directions["model_req_valid"] == "output"
    assert directions["model_rsp_valid"] == "input"


def test_ambiguous_top_output_producers_are_not_silently_connected():
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "ports": [_port("result", "output", 8)]},
            "modules": [
                {**_module("a"), "ports": [_port("result", "output", 8)]},
                {**_module("b"), "ports": [_port("result", "output", 8)]},
            ],
        },
        "top_level_connections": [],
    }

    out = spec_agent._ensure_hierarchical_top_level_connections(spec)

    assert not out.get("top_level_connections")


def test_feature_and_hierarchy_failures_are_reported_in_one_validation_pass():
    producer = {
        **_module("producer"),
        "rtl_output_file": "producer.v",
        "ports": [_port("clk", "input"), _port("status", "output"), _port("generated", "output")],
    }
    consumer = {
        **_module("consumer"),
        "rtl_output_file": "consumer.v",
        "ports": [_port("generated", "input"), _port("orphan", "input")],
    }
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"), "rtl_output_file": "top.v",
                "ports": [_port("clk", "input"), _port("status", "output")],
            },
            "modules": [producer, consumer],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["producer.clk"]},
            {"top_port": "status", "connected_to": ["producer.status"]},
        ],
        "inter_module_signals": [{
            "name": "generated", "width": 1, "source": "producer.generated",
            "destinations": ["consumer.generated"],
        }],
        "signal_ownership": [{"signal": "status", "owner": "producer.status"}],
        "feature_contracts": [{
            "id": "weak_status", "stimulus": {"clk": 1},
            "expected": {"status": {"min": 0, "max": 1}},
        }],
    }

    with pytest.raises(ValueError) as caught:
        spec_agent._validate_spec_contract(spec, "hierarchical", require_feature_contracts=True)

    message = str(caught.value)
    assert "full-domain min/max expectations" in message
    assert "consumer.orphan" in message


def test_sanitizer_drops_ownership_for_nonexistent_top_port():
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("uart_packet_engine"),
                "ports": [_port("clk", "input"), _port("irq", "output")],
            },
            "modules": [],
        },
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["uart_packet_engine.clk"]},
            {"top_port": "irq", "connected_to": ["uart_packet_engine.irq"]},
        ],
        "inter_module_signals": [],
        "signal_ownership": [
            {"signal": "internal_status_o", "owner": "uart_packet_engine.internal_status_o"},
            {"signal": "irq", "owner": "uart_packet_engine.irq"},
        ],
    }

    closed = spec_agent._ensure_hierarchical_port_closure(spec)
    out = spec_agent._sanitize_hierarchical_connectivity(closed)

    assert {port["name"] for port in out["hierarchy"]["top_module"]["ports"]} == {"clk", "irq"}
    assert out["signal_ownership"] == [{"signal": "irq", "owner": "uart_packet_engine.irq"}]


def test_requested_top_module_overrides_mmio_suffix_in_flat_spec():
    spec = {
        "name": "pwm_controller_mmio",
        "description": "PWM controller with register interface.",
        "ports": [],
        "functionality": "Generate PWM.",
        "responsibilities": [],
        "must_drive": [],
        "must_receive": [],
        "must_not_drive": [],
        "reset_behavior": "",
        "behavior_rules": [],
        "rtl_output_file": "pwm_controller_mmio.v",
    }

    out = spec_agent._apply_requested_top_module(spec, "flat", "pwm_controller")

    assert out["name"] == "pwm_controller"
    assert out["rtl_output_file"] == "pwm_controller.v"


def test_requested_top_module_overrides_mmio_suffix_in_hierarchical_spec():
    spec = {
        "design_name": "pwm_controller_mmio",
        "hierarchy": {
            "top_module": {
                "name": "pwm_controller_mmio",
                "ports": [],
                "rtl_output_file": "pwm_controller_mmio.v",
            },
            "modules": [{"name": "pwm_core", "ports": [], "rtl_output_file": "pwm_core.v"}],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["pwm_controller_mmio.clk"]}],
        "inter_module_signals": [
            {"name": "tick", "width": 1, "source": "pwm_controller_mmio.tick", "destinations": ["pwm_core.tick"]}
        ],
        "signal_ownership": [{"signal": "tick", "owner": "pwm_controller_mmio.tick"}],
    }

    out = spec_agent._apply_requested_top_module(spec, "hierarchical", "pwm_controller")

    assert out["design_name"] == "pwm_controller"
    assert out["hierarchy"]["top_module"]["name"] == "pwm_controller"
    assert out["hierarchy"]["top_module"]["rtl_output_file"] == "pwm_controller.v"
    assert out["top_level_connections"][0]["connected_to"] == ["pwm_controller.clk"]
    assert out["inter_module_signals"][0]["source"] == "pwm_controller.tick"
    assert out["signal_ownership"][0]["owner"] == "pwm_controller.tick"


def test_parse_prefers_nested_hierarchy_object_over_flat_child_module():
    raw = """
prefix text
{
  "top_module": {
    "name": "sram_mbist_demo_controller",
    "ports": [{"name": "clk", "direction": "input", "width": 1}],
    "rtl_output_file": "sram_mbist_demo_controller.v"
  },
  "modules": [
    {
      "name": "demo_sram_32x256_model",
      "ports": [{"name": "dout", "direction": "output", "width": 32}],
      "rtl_output_file": "demo_sram_32x256_model.v"
    }
  ],
  "top_level_connections": [{"top_port": "clk", "connected_to": ["sram_mbist_demo_controller.clk"]}],
  "inter_module_signals": [],
  "signal_ownership": []
}
{
  "name": "demo_sram_32x256_model",
  "ports": [{"name": "dout", "direction": "output", "width": 32}],
  "rtl_output_file": "demo_sram_32x256_model.v"
}
"""

    parsed = spec_agent._parse_llm_json_object(raw)

    assert "hierarchy" in parsed
    assert parsed["hierarchy"]["top_module"]["name"] == "sram_mbist_demo_controller"
    assert parsed["hierarchy"]["modules"][0]["name"] == "demo_sram_32x256_model"


def test_requested_top_rejects_flat_memory_interface_contract(tmp_path):
    llm_output = json.dumps(
        {
            "name": "demo_sram_32x256_wrapper",
            "description": "Synthesizable fallback memory model with macro-facing wrapper interface.",
            "memory_macros": [
                {
                    "name": "sky130_sram_1kbyte_1rw1r_32x256_8",
                    "depth": 256,
                    "data_width": 32,
                    "addr_width": 8,
                    "requires_mbist": True,
                    "ports": {
                        "clk": "clk",
                        "csb": "csb",
                        "we": "web",
                        "addr": "addr",
                        "din": "din",
                        "dout": "dout",
                    },
                }
            ],
            "ports": [
                _port("clk", "input"),
                _port("csb", "input"),
                _port("web", "input"),
                _port("addr", "input", 8),
                _port("din", "input", 32),
                _port("dout", "output", 32),
            ],
            "functionality": "SRAM wrapper fallback model.",
            "responsibilities": [],
            "must_drive": ["dout"],
            "must_receive": ["clk", "csb", "web", "addr", "din"],
            "must_not_drive": ["clk", "csb", "web", "addr", "din"],
            "reset_behavior": "",
            "behavior_rules": [],
            "rtl_output_file": "demo_sram_32x256_wrapper.v",
        }
    )

    with pytest.raises(ValueError, match="memory macro interface contract"):
        spec_agent._compile_spec_contract(
            llm_output,
            str(tmp_path),
            requested_top="sram_mbist_demo_controller",
        )


def test_normalize_accepts_hierarchical_modules_alias_and_preserves_top_dirs():
    spec = {
        "design_name": "sram_mbist_demo_controller",
        "hierarchy": {
            "top_module": {
                "name": "sram_mbist_demo_controller",
                "rtl_output_file": "sram_mbist_demo_controller.v",
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "rd_data", "direction": "output", "width": 32},
                ],
            }
        },
        "hierarchical_modules": [
            {
                "name": "demo_sram_32x256_wrapper",
                "rtl_output_file": "demo_sram_32x256_wrapper.v",
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "dout", "direction": "output", "width": 32},
                ],
            }
        ],
        "inter_module_signals": [
            {
                "name": "sram_clk",
                "width": 1,
                "source": "sram_mbist_demo_controller.clk",
                "destinations": ["demo_sram_32x256_wrapper.clk"],
            },
            {
                "name": "sram_dout",
                "width": 32,
                "source": "demo_sram_32x256_wrapper.dout",
                "destinations": ["sram_mbist_demo_controller.rd_data"],
            },
        ],
        "signal_ownership": [
            {"signal": "sram_clk", "owner": "sram_mbist_demo_controller.clk"},
            {"signal": "sram_dout", "owner": "demo_sram_32x256_wrapper.dout"},
        ],
        "top_level_connections": [
            {"top_port": "clk", "connected_to": ["sram_mbist_demo_controller.clk"]},
            {"top_port": "rd_data", "connected_to": ["sram_mbist_demo_controller.rd_data"]},
        ],
    }

    norm, mode = spec_agent._normalize_spec_json(spec)
    norm = spec_agent._reconcile_hierarchical_signal_directions(norm, mode)

    assert [m["name"] for m in norm["hierarchy"]["modules"]] == ["demo_sram_32x256_wrapper"]
    top_ports = {p["name"]: p["direction"] for p in norm["hierarchy"]["top_module"]["ports"]}
    assert top_ports["clk"] == "input"
    assert top_ports["rd_data"] == "output"
    spec_agent._validate_spec_contract(norm, mode)


def test_contract_rejects_required_child_input_without_structural_source():
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"), "rtl_output_file": "top.v",
                "ports": [_port("clk", "input")],
            },
            "modules": [{
                **_module("consumer"), "rtl_output_file": "consumer.v",
                "ports": [_port("clk", "input"), _port("result", "output")],
                "must_receive": ["clk"], "must_drive": ["result"], "must_not_drive": ["clk"],
            }],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["top.clk"]}],
        "inter_module_signals": [{
            "name": "result", "width": 1, "source": "consumer.result", "destinations": ["top.clk"]
        }],
        "signal_ownership": [{"signal": "result", "owner": "consumer.result"}],
    }

    with pytest.raises(ValueError, match="Required child input 'consumer.clk' has no source"):
        spec_agent._validate_spec_contract(spec, "hierarchical")


def test_contract_reports_all_required_child_inputs_without_sources():
    spec = {
        "hierarchy": {
            "top_module": {
                **_module("top"), "rtl_output_file": "top.v", "ports": [_port("clk", "input")],
            },
            "modules": [{
                **_module("consumer"),
                "rtl_output_file": "consumer.v",
                "ports": [
                    _port("status_valid_in", "input"),
                    _port("status_data_in", "input", 8),
                    _port("result", "output"),
                ],
                "must_receive": ["status_valid_in", "status_data_in"],
                "must_drive": ["result"],
                "must_not_drive": ["status_valid_in", "status_data_in"],
            }],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["top.clk"]}],
        "inter_module_signals": [{
            "name": "result", "width": 1, "source": "consumer.result", "destinations": ["top.clk"],
        }],
        "signal_ownership": [{"signal": "result", "owner": "consumer.result"}],
    }

    with pytest.raises(ValueError) as exc_info:
        spec_agent._validate_spec_contract(spec, "hierarchical")

    message = str(exc_info.value)
    assert "'consumer.status_valid_in'" in message
    assert "'consumer.status_data_in'" in message
    assert "Repair every listed input in the same response" in message


def test_flat_normalization_preserves_executable_feature_contracts():
    feature = {
        "id": "enable_done", "stimulus": {"enable": 1},
        "expected": {"done": 1}, "within_cycles": 2,
    }
    normalized, mode = spec_agent._normalize_spec_json({
        "name": "feature_top", "ports": [_port("enable", "input"), _port("done", "output")],
        "feature_contracts": [feature],
    })
    assert mode == "flat"
    assert normalized["feature_contracts"] == [feature]
    spec_agent._validate_spec_contract(normalized, mode, require_feature_contracts=True)


def test_validate_spec_rejects_reset_prose_that_conflicts_with_feature_expected_output():
    spec = {
        "name": "controller",
        "description": "Controller",
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset_n", "direction": "input", "width": 1},
            {"name": "data_out", "direction": "output", "width": 1},
        ],
        "responsibilities": ["Drive data_out."],
        "must_drive": ["data_out"],
        "must_receive": ["clk", "reset_n"],
        "must_not_drive": ["clk", "reset_n"],
        "reset_behavior": "When reset_n is low, data_out is low.",
        "behavior_rules": ["Drive data_out from current inputs."],
        "rtl_output_file": "controller.v",
        "feature_contracts": [{
            "id": "reset_observation",
            "description": "Observe reset behavior.",
            "stimulus": {"steps": [{"signals": {"reset_n": 0}, "cycles": 1}]},
            "expected": {"data_out": 1},
        }],
    }

    with pytest.raises(ValueError, match="Reset behavior contradicts executable feature contracts"):
        spec_agent._validate_spec_contract(spec, "flat", require_feature_contracts=True)


def test_reset_consistency_uses_reset_state_at_feature_observation_point():
    spec = {
        "name": "controller",
        "functionality": "Drive data_out after reset release.",
        "ports": [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1, "active_low": True},
            {"name": "data_out", "direction": "output", "width": 1},
        ],
        "responsibilities": ["Drive data_out."],
        "must_drive": ["data_out"],
        "must_receive": ["clk", "reset"],
        "must_not_drive": ["clk", "reset"],
        "reset_behavior": "When reset is asserted, data_out is low.",
        "behavior_rules": ["After reset, data_out may become high."],
        "rtl_output_file": "controller.v",
        "feature_contracts": [{
            "id": "post_reset_operation",
            "description": "Operate after reset release.",
            "stimulus": {"steps": [
                {"signals": {"reset": 0}, "cycles": 1},
                {"signals": {"reset": 1}, "cycles": 1},
            ]},
            "expected": {"data_out": 1},
        }],
    }

    spec_agent._validate_spec_contract(spec, "flat", require_feature_contracts=True)


def test_terminal_graph_closure_exposes_and_fans_out_orphan_child_inputs():
    spec = {
        "hierarchy": {
            "top_module": {**_module("top"), "rtl_output_file": "top.v", "ports": []},
            "modules": [
                {**_module("a"), "rtl_output_file": "a.v", "ports": [_port("clk", "input"), _port("data_i", "input", 8)]},
                {**_module("b"), "rtl_output_file": "b.v", "ports": [_port("clk", "input")]},
            ],
        },
        "top_level_connections": [],
        "inter_module_signals": [],
        "signal_ownership": [{"signal": "placeholder", "owner": "top.placeholder"}],
    }

    out = spec_agent._expose_orphan_child_inputs_at_top(spec)

    ports = {port["name"]: port for port in out["hierarchy"]["top_module"]["ports"]}
    assert ports["clk"] == {"name": "clk", "direction": "input", "width": 1}
    assert ports["data_i"] == {"name": "data_i", "direction": "input", "width": 8}
    connections = {item["top_port"]: item["connected_to"] for item in out["top_level_connections"]}
    assert connections["clk"] == ["a.clk", "b.clk"]
    assert connections["data_i"] == ["a.data_i"]


def test_contract_rejects_unconsumed_required_memory_read_data():
    spec = {
        "memory_macros": [{
            "name": "history_bram", "ports": {"clk": "clk", "dout": "rdata"},
        }],
        "hierarchy": {
            "top_module": {**_module("top"), "rtl_output_file": "top.v", "ports": [_port("clk", "input")]},
            "modules": [{
                **_module("history_bram"), "rtl_output_file": "history_bram.v",
                "ports": [_port("clk", "input"), _port("rdata", "output", 32)],
            }],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["history_bram.clk"]}],
        "inter_module_signals": [{
            "name": "placeholder", "width": 1, "source": "top.clk", "destinations": ["history_bram.clk"],
        }],
        "signal_ownership": [{"signal": "placeholder", "owner": "top.clk"}],
    }

    with pytest.raises(ValueError, match="history_bram.rdata is unconsumed"):
        spec_agent._validate_spec_contract(spec, "hierarchical")


def test_contract_accepts_required_memory_read_data_consumed_by_child():
    spec = {
        "memory_macros": [{
            "name": "history_bram", "ports": {"clk": "clk", "dout": "rdata"},
        }],
        "hierarchy": {
            "top_module": {**_module("top"), "rtl_output_file": "top.v", "ports": [_port("clk", "input")]},
            "modules": [
                {**_module("history_bram"), "rtl_output_file": "history_bram.v", "ports": [_port("clk", "input"), _port("rdata", "output", 32)]},
                {**_module("reader"), "rtl_output_file": "reader.v", "ports": [_port("rdata", "input", 32)]},
            ],
        },
        "top_level_connections": [{"top_port": "clk", "connected_to": ["history_bram.clk"]}],
        "inter_module_signals": [{
            "name": "memory_rdata", "width": 32, "source": "history_bram.rdata", "destinations": ["reader.rdata"],
        }],
        "signal_ownership": [{"signal": "memory_rdata", "owner": "history_bram.rdata"}],
    }

    spec_agent._validate_spec_contract(spec, "hierarchical")


def test_feature_contract_strength_rejects_full_signal_domain():
    ports = [{"name": "ready", "direction": "output", "width": 1}]
    contracts = [{"feature_id": "flow_control", "expected": {"ready": {"min": 0, "max": 1}}}]

    with pytest.raises(ValueError, match="behavior-discriminating"):
        spec_agent._validate_feature_contract_strength(ports, contracts)


def test_feature_contract_strength_accepts_exact_or_strict_subrange():
    ports = [{"name": "ready", "direction": "output", "width": 1}, {"name": "count", "direction": "output", "width": 8}]
    contracts = [
        {"feature_id": "blocked", "expected": {"ready": {"eq": 0}}},
        {"feature_id": "bounded_count", "expected": {"count": {"min": 2, "max": 12}}},
    ]

    spec_agent._validate_feature_contract_strength(ports, contracts)


def test_feature_contract_strength_rejects_unestablished_one_cycle_state_precondition():
    ports = [
        {"name": "threshold", "direction": "input", "width": 8},
        {"name": "state_value", "direction": "output", "width": 8},
        {"name": "flag", "direction": "output", "width": 1},
    ]
    contracts = [{
        "feature_id": "state_condition",
        "statement": "The flag deasserts when state_value is greater than or equal to threshold.",
        "stimulus_cycles": 1,
        "expected": {"flag": 0},
    }]
    with pytest.raises(ValueError, match="state-dependent expectations"):
        spec_agent._validate_feature_contract_strength(ports, contracts)


def test_reset_consistency_does_not_attribute_pronoun_value_to_wrong_signal():
    spec = {
        "reset_behavior": (
            "When reset_n is low at a clock edge, counter_value is set to 0. "
            "Because pwm_out is purely combinational from counter_value and duty_cycle, "
            "it evaluates high when duty_cycle is nonzero."
        ),
    }
    ports = [
        {"name": "reset_n", "direction": "input", "active_low": True},
        {"name": "duty_cycle", "direction": "input", "width": 8},
        {"name": "counter_value", "direction": "output", "width": 8},
        {"name": "pwm_out", "direction": "output", "width": 1},
    ]
    contracts = [{
        "feature_id": "reset_clears_counter",
        "stimulus_steps": [{"signals": {"reset_n": 0, "duty_cycle": 8}, "cycles": 1}],
        "expected": {"counter_value": 0, "pwm_out": 1},
    }]
    spec_agent._validate_reset_feature_consistency(spec, ports, contracts)


def test_reset_consistency_rejects_expected_value_that_contradicts_declared_comparison():
    spec = {
        "reset_behavior": (
            "When reset_n is low, counter_value is set to zero. Consequently pwm_out evaluates "
            "according to the comparison 0 < duty_cycle, which is true for nonzero duty_cycle."
        ),
    }
    ports = [
        {"name": "reset_n", "direction": "input", "active_low": True},
        {"name": "duty_cycle", "direction": "input", "width": 8},
        {"name": "counter_value", "direction": "output", "width": 8},
        {"name": "pwm_out", "direction": "output", "width": 1},
    ]
    contracts = [{
        "feature_id": "reset_clears_state",
        "stimulus_steps": [{"signals": {"reset_n": 0, "duty_cycle": 128}, "cycles": 1}],
        "expected": {"counter_value": 0, "pwm_out": 0},
    }]
    with pytest.raises(ValueError, match=r"pwm_out.*0 < duty_cycle=1.*expected is 0"):
        spec_agent._validate_reset_feature_consistency(spec, ports, contracts)


def test_feature_contract_requires_enable_write_before_request_when_reset_disabled():
    spec = {"register_contract": {"registers": [{
        "name": "CTRL", "address": 0,
        "fields": [{
            "name": "enable", "lsb": 0, "reset": 0,
            "description": "Global enable for request acceptance and command propagation.",
        }],
    }]}}
    ports = [
        {"name": "csr_valid", "direction": "input"},
        {"name": "csr_write", "direction": "input"},
        {"name": "csr_addr", "direction": "input"},
        {"name": "csr_wdata", "direction": "input"},
        {"name": "req_valid", "direction": "input"},
        {"name": "req_ready", "direction": "output"},
    ]
    bad = [{
        "feature_id": "accept_request",
        "stimulus_steps": [{"signals": {"req_valid": 1}, "cycles": 1}],
        "expected": {"req_ready": 1},
    }]
    with pytest.raises(ValueError, match="prior CSR/MMIO enable write"):
        spec_agent._validate_feature_contract_feasibility(spec, ports, bad)

    good = [{
        "feature_id": "accept_request",
        "stimulus_steps": [
            {"signals": {"csr_valid": 1, "csr_write": 1, "csr_addr": 0, "csr_wdata": 1}, "cycles": 1},
            {"signals": {"req_valid": 1}, "cycles": 1},
        ],
        "expected": {"req_ready": 1},
    }]
    spec_agent._validate_feature_contract_feasibility(spec, ports, good)


def test_feature_contract_feasibility_accepts_hex_register_address_and_reset():
    spec = {"register_contract": {"registers": [{
        "name": "CTRL", "address": "0x0",
        "fields": [{
            "name": "enable", "lsb": 0, "reset": "0x0",
            "description": "Global enable for request acceptance and command propagation.",
        }],
    }]}}
    ports = [
        {"name": "csr_valid", "direction": "input"},
        {"name": "csr_write", "direction": "input"},
        {"name": "csr_addr", "direction": "input"},
        {"name": "csr_wdata", "direction": "input"},
        {"name": "req_valid", "direction": "input"},
        {"name": "req_ready", "direction": "output"},
    ]
    contracts = [{
        "feature_id": "accept_request",
        "stimulus_steps": [
            {"signals": {"csr_valid": 1, "csr_write": 1, "csr_addr": 0, "csr_wdata": 1}, "cycles": 1},
            {"signals": {"req_valid": 1}, "cycles": 1},
        ],
        "expected": {"req_ready": 1},
    }]

    spec_agent._validate_feature_contract_feasibility(spec, ports, contracts)


def test_feature_contract_strength_rejects_weak_signal_even_with_strong_signal():
    ports = [{"name": "valid", "width": 1}, {"name": "data", "width": 8}]
    contracts = [{
        "feature_id": "response",
        "expected": {"valid": {"eq": 1}, "data": {"min": 0, "max": 255}},
    }]

    with pytest.raises(ValueError, match="response: data"):
        spec_agent._validate_feature_contract_strength(ports, contracts)


def test_feature_strength_repair_cannot_rewrite_architecture():
    previous = {
        "design_name": "top",
        "hierarchy": {
            "top_module": {"name": "top", "ports": [_port("ready", "output")]},
            "modules": [{"name": "core", "ports": [_port("ready", "output")]}],
        },
        "feature_contracts": [{
            "id": "ready", "stimulus": {}, "expected": {"ready": {"min": 0, "max": 1}},
        }],
    }
    repair = {
        "design_name": "redesigned_top",
        "hierarchy": {"top_module": {"name": "redesigned_top", "ports": []}, "modules": []},
        "feature_contracts": [{"id": "ready", "stimulus": {}, "expected": {"ready": 1}}],
    }

    merged = json.loads(spec_agent._merge_feature_strength_repair(
        json.dumps(previous), json.dumps(repair),
    ))

    assert merged["design_name"] == "top"
    assert merged["hierarchy"] == previous["hierarchy"]
    assert merged["feature_contracts"] == repair["feature_contracts"]


def test_feature_strength_failure_classifier_does_not_capture_graph_failures():
    assert spec_agent._is_feature_strength_only_failure(
        "Feature contracts must contain behavior-discriminating checkers; "
        "full-domain min/max expectations accept every possible output"
    ) is True
    assert spec_agent._is_feature_strength_only_failure(
        "Hierarchical graph validation failed: Required child input has no source"
    ) is False
    assert spec_agent._is_feature_strength_only_failure(
        "Feature contracts must contain behavior-discriminating checkers; "
        "full-domain min/max expectations accept every possible output | "
        "FPGA memory contract requires a technology-neutral wrapper"
    ) is False
