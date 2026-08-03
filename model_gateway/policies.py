import os
from typing import Any, Dict


MODEL_MODE_STANDARD = "standard"
MODEL_MODE_SMART = "smart"
STANDARD_MODEL_DEFAULT = "chiploop_default"
STANDARD_MODEL_NEMOTRON = "nvidia_nemotron"


def normalize_model_policy(value: Any) -> Dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or MODEL_MODE_STANDARD).strip().lower()
    if mode not in {MODEL_MODE_STANDARD, MODEL_MODE_SMART}:
        mode = MODEL_MODE_STANDARD
    selected = str(raw.get("selected_model") or STANDARD_MODEL_DEFAULT).strip().lower()
    if selected not in {STANDARD_MODEL_DEFAULT, STANDARD_MODEL_NEMOTRON}:
        selected = STANDARD_MODEL_DEFAULT
    return {"mode": mode, "selected_model": selected, "overrides": raw.get("overrides") or {}}


def _nemotron_route() -> Dict[str, Any]:
    return {
        "provider": "openai_compatible",
        "model": os.getenv("NVIDIA_NEMOTRON_MODEL", "nvidia/nemotron-3-nano-30b-a3b"),
        "base_url": os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key_env": "NVIDIA_API_KEY",
        "stream": True,
    }


def apply_model_policy(profile: Dict[str, Any], policy_value: Any) -> Dict[str, Any]:
    policy = normalize_model_policy(policy_value)
    if policy["mode"] == MODEL_MODE_STANDARD and policy["selected_model"] == STANDARD_MODEL_DEFAULT:
        profile["model_policy"] = policy
        return profile

    routing = dict(profile.get("routing") or {})
    agents = dict(profile.get("agents") or {})
    nemotron = _nemotron_route()

    if policy["mode"] == MODEL_MODE_STANDARD:
        for capability, route in list(routing.items()):
            if capability == "embeddings":
                continue
            routing[capability] = {**(route if isinstance(route, dict) else {}), **nemotron}
    else:
        # Smart v1 is deterministic and auditable. ChipLoop's measured model
        # evaluations can replace these seed rules without changing callers.
        for capability in ("planner", "spec_generation", "inspection", "analog_generation"):
            routing[capability] = {**(routing.get(capability) or {}), **nemotron}

    for agent_name, selected in policy["overrides"].items():
        if selected == STANDARD_MODEL_NEMOTRON:
            agents[str(agent_name)] = {**(agents.get(str(agent_name)) or {}), **nemotron}

    profile["routing"] = routing
    profile["agents"] = agents
    profile["model_policy"] = policy
    return profile


def physical_ai_agent_assignments(policy_value: Any) -> Dict[str, Dict[str, str]]:
    policy = normalize_model_policy(policy_value)
    names = [
        "Requirements Agent", "Physics Surrogate Agent", "Model Compression Agent",
        "FPGA Architecture Agent", "RTL Agent", "Verification Agent",
        "Firmware Agent", "Hardware Validation Agent",
    ]
    if policy["mode"] == MODEL_MODE_STANDARD:
        model = policy["selected_model"]
        return {name: {"model": model, "reason": "Standard mode applies one model to every agent."} for name in names}

    nemotron_agents = {"Requirements Agent", "Physics Surrogate Agent", "FPGA Architecture Agent"}
    return {
        name: {
            "model": STANDARD_MODEL_NEMOTRON if name in nemotron_agents else STANDARD_MODEL_DEFAULT,
            "reason": "Smart v1 routes physics planning to Nemotron and implementation/verification to ChipLoop Default.",
        }
        for name in names
    }
