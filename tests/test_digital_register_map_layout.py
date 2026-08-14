import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

import pytest

from agents.digital import digital_register_map_agent
from agents.digital.digital_register_map_agent import (
    _register_layout_violations,
    _repair_overlapping_fields_deterministically,
)


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


def test_deterministic_repair_places_mbist_address_in_free_aligned_byte():
    document = {
        "regmap": {
            "data_width": 32,
            "registers": [{
                "name": "BIST_STATUS",
                "offset": "0x1C",
                "fields": [
                    {"name": "done", "lsb": 0, "msb": 0, "access": "RO"},
                    {"name": "fail", "lsb": 1, "msb": 1, "access": "RO"},
                    {"name": "running", "lsb": 2, "msb": 2, "access": "RO"},
                    {"name": "last_fail_addr", "lsb": 0, "msb": 7, "access": "RO"},
                ],
            }],
        },
    }

    repaired, changed = _repair_overlapping_fields_deterministically(document)

    assert changed is True
    assert _register_layout_violations(repaired) == []
    last_fail = repaired["regmap"]["registers"][0]["fields"][3]
    assert (last_fail["msb"], last_fail["lsb"]) == (15, 8)
    assert last_fail["access"] == "RO"


def test_arch2rtl_uses_deterministic_overlap_repair_without_second_llm_call(tmp_path, monkeypatch):
    invalid = {
        "derived_from_spec_only": True,
        "regmap": {
            "data_width": 32,
            "registers": [{
                "name": "BIST_STATUS", "offset": "0x1C", "fields": [
                    {"name": "done", "lsb": 0, "msb": 0},
                    {"name": "fail", "lsb": 1, "msb": 1},
                    {"name": "running", "lsb": 2, "msb": 2},
                    {"name": "last_fail_addr", "lsb": 0, "msb": 7},
                ],
            }],
        },
    }
    calls = []
    monkeypatch.setattr(
        digital_register_map_agent,
        "complete_text",
        lambda *_args, **_kwargs: calls.append(1) or __import__("json").dumps(invalid),
    )
    monkeypatch.setattr(digital_register_map_agent, "save_text_artifact_and_record", lambda **_kwargs: None)
    state = {
        "workflow_id": "mbist-overlap",
        "workflow_dir": str(tmp_path),
        "digital_spec_json": {
            "name": "mbist", "rtl_output_file": "mbist.sv",
            "ports": [{"name": "cfg_addr"}, {"name": "cfg_we"}, {"name": "cfg_wdata"}],
        },
    }

    digital_register_map_agent.run_agent(state)

    assert len(calls) == 1
    assert state["digital_regmap_layout_repair_method"] == "deterministic_free_bit_placement"
    assert _register_layout_violations(state["digital_regmap"]) == []


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


def test_csr_wen_ren_bus_requires_model_generated_register_map():
    spec = {
        "name": "adaptive_controller",
        "rtl_output_file": "adaptive_controller.v",
        "ports": [
            {"name": "clk", "direction": "input"},
            {"name": "csr_addr", "direction": "input", "width": 8},
            {"name": "csr_wdata", "direction": "input", "width": 64},
            {"name": "csr_wen", "direction": "input"},
            {"name": "csr_ren", "direction": "input"},
            {"name": "csr_rdata", "direction": "output", "width": 64},
            {"name": "csr_ready", "direction": "output"},
        ],
        "register_contract": {},
    }

    assert digital_register_map_agent._spec_requires_register_map(spec) is True
