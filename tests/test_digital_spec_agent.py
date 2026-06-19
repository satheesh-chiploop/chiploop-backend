import os
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
    spec_agent._validate_spec_contract(out, mode)

    memory_module = out["hierarchy"]["modules"][0]
    assert memory_module["name"] == "demo_sram_32x64_model"
    assert memory_module["rtl_output_file"] == "demo_sram_32x64_model.v"
    assert {p["name"]: p["width"] for p in memory_module["ports"]}["addr"] == 6
    assert {p["name"]: p["width"] for p in memory_module["ports"]}["din"] == 32


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
