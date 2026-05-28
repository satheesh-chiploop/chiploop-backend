import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_cosim_trace_validation_agent as agent


def test_boot_signal_expectation_is_not_required_without_signal_artifacts(monkeypatch):
    monkeypatch.setattr(agent, "_record_text", lambda *args, **kwargs: None)

    state = {
        "workflow_id": "wf-test",
        "system_software_cosim_harness_manifest": {
            "scenarios": [{"id": "fan_profile_service_boot_smoke", "enabled": True}]
        },
        "system_software_cosim_execution_report": {
            "scenario_results": [
                {
                    "scenario_id": "fan_profile_service_boot_smoke",
                    "execution_status": "pass",
                    "expected_behavior": {
                        "expected_events": ["app=fan_profile_service", "scenario=fan_profile_service_boot_smoke"],
                        "expected_registers": {},
                        "expected_interrupts": [],
                        "expected_signals": ["reset_released"],
                    },
                    "observed_behavior": {
                        "observed_events": [
                            "app=fan_profile_service",
                            "scenario=fan_profile_service_boot_smoke",
                            "status=Ok(DeviceStatus { healthy: false })",
                        ],
                        "observed_registers": {},
                        "observed_interrupts": [],
                        "observed_signals": [],
                    },
                    "artifacts": {"waveform": None, "rtl_log": None, "firmware_log": None},
                }
            ]
        },
    }

    result = agent.run_agent(state)
    report = result["system_software_cosim_trace_validation_report"]

    assert report["trace_validation_status"] == "pass"
    assert report["scenario_pass_count"] == 1
    assert report["scenario_fail_count"] == 0
    assert report["scenario_validations"][0]["mismatches"] == []
