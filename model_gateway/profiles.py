import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_MODEL_PROFILE_ID = "chiploop_saas_default"
DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.5"


def _default_profile() -> Dict[str, Any]:
    return {
        "model_profile_id": DEFAULT_MODEL_PROFILE_ID,
        "provider": DEFAULT_MODEL_PROVIDER,
        "routing": {
            "default": {"model": DEFAULT_MODEL, "stream": True},
            "planner": {"model": DEFAULT_MODEL, "stream": True},
            "rtl_generation": {"model": DEFAULT_MODEL, "max_completion_tokens": 32000, "timeout_sec": 360, "max_retries": 2, "stream": True},
            "spec_generation": {"model": DEFAULT_MODEL, "max_completion_tokens": 24000, "timeout_sec": 150, "max_retries": 1, "stream": True},
            "verification_debug": {"model": DEFAULT_MODEL, "stream": True},
            "embedded_generation": {"model": DEFAULT_MODEL, "stream": True},
            "analog_generation": {"model": DEFAULT_MODEL, "stream": True},
            "summarizer": {"model": DEFAULT_MODEL, "stream": True},
            "doc_generation": {"model": DEFAULT_MODEL, "stream": True},
            "inspection": {"model": DEFAULT_MODEL, "stream": True, "max_completion_tokens": 4096, "timeout_sec": 150, "max_retries": 1},
            "embeddings": {"model": "text-embedding-3-small"},
        },
        "agents": {},
    }


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=8)
def _load_profile_file(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def get_model_profile(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state or {}
    profile = state.get("model_profile")
    if isinstance(profile, dict):
        return _deep_merge(_default_profile(), profile)

    profile_path = (
        state.get("model_profile_path")
        or os.getenv("CHIPLOOP_MODEL_PROFILE_PATH")
    )
    if isinstance(profile_path, str) and profile_path and os.path.exists(profile_path):
        return _deep_merge(_default_profile(), _load_profile_file(profile_path))

    return _default_profile()


def resolve_route(profile: Dict[str, Any], capability: str, agent_name: Optional[str] = None) -> Dict[str, Any]:
    routing = profile.get("routing") if isinstance(profile.get("routing"), dict) else {}
    route: Dict[str, Any] = {}
    base_route = routing.get(capability) or routing.get("default") or {}
    if isinstance(base_route, dict):
        route.update(base_route)

    agents = profile.get("agents") if isinstance(profile.get("agents"), dict) else {}
    agent_route = agents.get(agent_name or "") if agent_name else None
    if isinstance(agent_route, dict):
        route.update(agent_route)
        capability_route = agent_route.get(capability)
        if isinstance(capability_route, dict):
            route.update(capability_route)

    return route


def model_profile_summary(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = get_model_profile(state)
    routing = profile.get("routing") if isinstance(profile.get("routing"), dict) else {}
    return {
        "model_profile_id": profile.get("model_profile_id") or DEFAULT_MODEL_PROFILE_ID,
        "provider": profile.get("provider") or DEFAULT_MODEL_PROVIDER,
        "capabilities": sorted(routing.keys()),
        "agents": sorted((profile.get("agents") or {}).keys()) if isinstance(profile.get("agents"), dict) else [],
    }
