import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

import pytest

from agents.digital import digital_register_map_agent
from agents.digital.digital_register_map_agent import _register_layout_violations


def test_register_layout_rejects_fields_beyond_declared_bus_width():
    document = {
        "regmap": {
            "data_width": 64,
            "registers": [{
                "name": "CTRL_STATUS",
                "offset": "0x0",
                "fields": [
                    {"name": "CONTROL", "lsb": 0, "msb": 29},
                    {"name": "STATUS", "lsb": 69, "msb": 79},
                ],
            }],
        },
    }

    violations = _register_layout_violations(document)

    assert violations == ["CTRL_STATUS.STATUS [79:69] is outside the 64-bit register word"]


def test_register_layout_accepts_multiple_addressed_words():
    document = {
        "regmap": {
            "data_width": 64,
            "registers": [
                {"name": "CONTROL", "offset": "0x0", "fields": [{"name": "ENABLE", "lsb": 0, "msb": 0}]},
                {"name": "STATUS", "offset": "0x8", "fields": [{"name": "READY", "lsb": 0, "msb": 0}]},
            ],
        },
    }

    assert _register_layout_violations(document) == []


def test_register_layout_rejects_overlapping_fields():
    document = {
        "regmap": {
            "data_width": 32,
            "registers": [{
                "name": "CONTROL",
                "offset": "0x0",
                "fields": [
                    {"name": "MODE", "lsb": 0, "msb": 3},
                    {"name": "ENABLE", "lsb": 3, "msb": 3},
                ],
            }],
        },
    }

    assert _register_layout_violations(document) == ["CONTROL.ENABLE [3:3] overlaps MODE [3:0]"]


def test_arch2rtl_fails_hard_when_register_layout_repair_remains_invalid(tmp_path, monkeypatch):
    invalid = '{"regmap":{"data_width":64,"registers":[{"name":"CTRL_STATUS","offset":"0x0","fields":[{"name":"STATUS","lsb":69,"msb":79}]}]}}'
    monkeypatch.setattr(digital_register_map_agent, "complete_text", lambda *_args, **_kwargs: invalid)
    monkeypatch.setattr(digital_register_map_agent, "save_text_artifact_and_record", lambda **_kwargs: None)
    state = {
        "workflow_id": "invalid-regmap",
        "workflow_dir": str(tmp_path),
        "digital_spec_json": {
            "name": "demo", "rtl_output_file": "demo.sv",
            "ports": [{"name": "cfg_addr"}, {"name": "cfg_we"}, {"name": "cfg_wdata"}],
        },
    }

    with pytest.raises(RuntimeError, match="remains invalid after repair"):
        digital_register_map_agent.run_agent(state)

    assert state["digital_regmap_layout_violations"] == [
        "CTRL_STATUS.STATUS [79:69] is outside the 64-bit register word"
    ]


def test_self_contained_fpga_design_skips_register_map_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        digital_register_map_agent,
        "complete_text",
        lambda *_args, **_kwargs: pytest.fail("LLM must not invent registers for a design without a bus"),
    )
    monkeypatch.setattr(digital_register_map_agent, "save_text_artifact_and_record", lambda **_kwargs: None)
    state = {
        "workflow_id": "pwm-no-regmap",
        "workflow_dir": str(tmp_path),
        "digital_spec_json": {
            "name": "pwm_fpga_demo",
            "rtl_output_file": "pwm_fpga_demo.v",
            "ports": [{"name": "clk", "direction": "input"}, {"name": "led", "direction": "output"}],
            "register_contract": {},
        },
    }

    digital_register_map_agent.run_agent(state)

    assert state["register_map_required"] is False
    assert state["digital_regmap"]["regmap"]["status"] == "not_applicable"
    assert state["digital_regmap"]["regmap"]["registers"] == []
    assert _register_layout_violations(state["digital_regmap"]) == []


def test_cfg_bus_ports_require_register_map_even_without_explicit_contract():
    spec = {
        "name": "controlled_block",
        "rtl_output_file": "controlled_block.v",
        "ports": [
            {"name": "cfg_addr"}, {"name": "cfg_we"}, {"name": "cfg_wdata"}, {"name": "cfg_rdata"},
        ],
        "register_contract": {},
    }

    assert digital_register_map_agent._spec_requires_register_map(spec) is True
