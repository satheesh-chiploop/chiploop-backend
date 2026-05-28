import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.system import system_software_sdk_scaffold_agent as agent


def test_sdk_renderer_sanitizes_method_names_from_handoff_paths():
    rendered = agent._render_rust_lib(
        "pwm_fan_control_software",
        {
            "public_api_groups": [
                {
                    "name": "handoff_candidates",
                    "methods": [
                        {"name": "firmware/firmware_manifest.json", "inputs": [], "returns": "candidate"}
                    ],
                }
            ]
        },
    )

    assert "pub fn firmware_firmware_manifest(&self)" in rendered
    assert "pub fn firmware/firmware_manifest.json" not in rendered
