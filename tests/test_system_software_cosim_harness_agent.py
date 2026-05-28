import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_cosim_harness_agent as agent


def test_unknown_software_l1_readiness_does_not_block_harness(monkeypatch):
    monkeypatch.setattr(agent, "_record_text", lambda *args, **kwargs: None)
    state = {
        "workflow_id": "wf-test",
        "system_software_validation_manifest": {
            "discovered_assets": {
                "software_package_manifest": {"path": "system/software/package/system_software_package.json"},
                "api_contract": {"path": "system/software/sdk/system_software_api_contract.json"},
                "input_contract": {"path": "system/software/input/system_software_input_contract.json"},
            }
        },
        "system_cosim_scenarios": {
            "scenarios": [
                {
                    "id": "boot_smoke",
                    "software_target": {"cargo_package": "demo_app"},
                }
            ]
        },
        "system_cosim_manifest": {
            "firmware": {
                "elf": "firmware/build/firmware_app.elf",
                "register_map": "firmware/register_map.json",
            },
            "rtl": {
                "top": "pwm_controller",
                "filelists": {"sim": ["rtl/pwm_controller.v"]},
            },
            "validation_spec": {
                "rtl": {
                    "digital_spec_json": {"design_name": "pwm_controller"},
                    "integration_intent_json": {"intent_type": "system_integration"},
                }
            },
        },
        "firmware_register_map": {"registers": [{"name": "CONTROL"}]},
    }

    result = agent.run_agent(state)
    manifest = result["system_software_cosim_harness_manifest"]

    assert manifest["l1_ready"] is None
    assert manifest["harness_status"] == "ready"
    assert "software_l1_not_ready" not in manifest["blocked_dependencies"]


def test_explicit_failed_software_l1_readiness_blocks_harness():
    assert agent._read_l1_ready({"overall_status": "failed"}) is False
    assert agent._read_l1_ready({"l1_ready": False}) is False
