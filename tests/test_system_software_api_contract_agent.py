import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_api_contract_agent as agent


def test_public_api_candidates_are_sanitized_for_rust():
    contract = agent._build_api_contract(
        {
            "software_services": {},
            "public_api_candidates": [
                "firmware/firmware_manifest.json",
                "system/software/apps/fan-status-cli",
            ],
        },
        {"target_language": "rust"},
    )

    candidate_group = next(group for group in contract["public_api_groups"] if group["name"] == "handoff_candidates")
    names = [method["name"] for method in candidate_group["methods"]]

    assert "firmware_firmware_manifest" in names
    assert "system_software_apps_fan_status_cli" in names
    assert all("/" not in name and "." not in name and "-" not in name for name in names)
