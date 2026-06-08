import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.digital import digital_spec_agent as spec_agent


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
