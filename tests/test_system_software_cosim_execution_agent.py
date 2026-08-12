import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_software_cosim_execution_agent as agent


def test_generic_register_write_binds_to_expected_register_not_offset_alias():
    state = {
        "system_software_cosim_harness_manifest": {
            "firmware_assets": {
                "register_map_json": {
                    "registers": [
                        {"name": "CONTROL", "offset": "0x0", "access": "RW"},
                        {"name": "SAMPLE_COUNT", "offset": "0x10", "access": "RO"},
                    ]
                }
            }
        }
    }
    scenario = {
        "scenario_id": "tempmon_cli_register_rw_basic",
        "expected_registers": {"CONTROL": "0x10"},
    }
    raw_observations = {
        "observed_events": [
            "app=tempmon_cli",
            "scenario=tempmon_cli_register_rw_basic",
            "register_write=0x10",
        ],
        "observed_registers": {"SAMPLE_COUNT": "0x10"},
        "observed_interrupts": [],
        "observed_signals": [],
    }

    normalized = agent._normalize_observations(state, scenario, raw_observations)

    assert normalized["observed_registers"] == {"CONTROL": "0x10"}


def test_disabled_contract_scenarios_are_not_applicable_not_blocked(monkeypatch):
    monkeypatch.setattr(agent, "_record_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_run_cmd", lambda *_args, **_kwargs: {
        "returncode": 0,
        "stdout_tail": "app=control scenario=boot_smoke reset_released",
        "stderr_tail": "",
    })
    state = {
        "workflow_id": "validation",
        "system_software_cosim_harness_manifest": {
            "harness_status": "ready",
            "resolved_commands": [{
                "scenario_id": "boot_smoke",
                "command_id": "rtl",
                "command": ["make"],
                "source": "scenario.runner:verilator",
                "exercises_rtl": True,
            }],
            "scenarios": [
                {"scenario_id": "boot_smoke", "enabled": True},
                {"scenario_id": "interrupt_basic", "enabled": False},
            ],
        },
    }

    report = agent.run_agent(state)["system_software_cosim_execution_report"]

    assert report["execution_status"] == "pass"
    assert report["scenario_pass_count"] == 1
    assert report["scenario_blocked_count"] == 0
    assert report["scenario_not_applicable_count"] == 1
    assert report["scenario_results"][1]["execution_status"] == "not_applicable"
