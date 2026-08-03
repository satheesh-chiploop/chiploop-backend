from model_gateway.policies import apply_model_policy, physical_ai_agent_assignments
from model_gateway.profiles import get_model_profile


def test_standard_default_preserves_current_model():
    profile = get_model_profile({"model_policy": {"mode": "standard", "selected_model": "chiploop_default"}})
    assert profile["routing"]["default"]["model"] == "gpt-5.4-mini"


def test_standard_nemotron_routes_all_chat_capabilities():
    profile = get_model_profile({"model_policy": {"mode": "standard", "selected_model": "nvidia_nemotron"}})
    for capability, route in profile["routing"].items():
        if capability != "embeddings":
            assert route["provider"] == "openai_compatible"
            assert "nemotron" in route["model"]
            assert route["api_key_env"] == "NVIDIA_API_KEY"


def test_smart_policy_is_per_capability_and_explainable():
    profile = apply_model_policy(get_model_profile(), {"mode": "smart"})
    assert profile["routing"]["planner"]["provider"] == "openai_compatible"
    assert profile["routing"]["rtl_generation"]["model"] == "gpt-5.4-mini"
    assignments = physical_ai_agent_assignments({"mode": "smart"})
    assert assignments["Physics Surrogate Agent"]["model"] == "nvidia_nemotron"
    assert assignments["RTL Agent"]["model"] == "chiploop_default"
