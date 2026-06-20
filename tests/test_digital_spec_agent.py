import os
import json
import sys
from pathlib import Path

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


def test_parse_llm_json_object_prefers_last_spec_shaped_object():
    text = (
        '{"design_name":"draft","hierarchy":{"top_module":{"name":"draft"}}}'
        '\n'
        '{"design_name":"final","hierarchy":{"top_module":{"name":"final"}},"top_level_connections":[{"top_port":"clk"}]}'
    )

    parsed = spec_agent._parse_llm_json_object(text)

    assert parsed["design_name"] == "final"
    assert parsed["top_level_connections"][0]["top_port"] == "clk"


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
