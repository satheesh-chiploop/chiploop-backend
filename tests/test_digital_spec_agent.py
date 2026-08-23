import os
import json
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_spec_agent as spec_agent


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
