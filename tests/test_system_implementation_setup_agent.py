import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from agents.system import system_implementation_setup_agent as agent


def test_resolve_top_module_prefers_system_rtl_package_phys_top():
    state = {
        "system_rtl_package": {
            "top": {
                "sim": "temp_monitor_soc_sim",
                "phys": "temp_monitor_soc_phys",
            }
        },
        "digital": {"top_module": "top"},
    }

    assert agent._resolve_top_module(state, profile={}, spec={}) == "temp_monitor_soc_phys"
