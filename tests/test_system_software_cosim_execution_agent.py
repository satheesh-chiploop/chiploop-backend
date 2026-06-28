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
