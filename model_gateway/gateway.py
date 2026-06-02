import os
from typing import Any, Dict, List, Optional

import httpx
from openai import AzureOpenAI, OpenAI

try:
    from portkey_ai import Portkey
except Exception:  # pragma: no cover - optional dependency in private installs
    Portkey = None

from .profiles import get_model_profile, model_profile_summary, resolve_route


def _messages(prompt: str, system: str = "") -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if system:
        out.append({"role": "system", "content": system})
    out.append({"role": "user", "content": prompt})
    return out


def _env_value(profile: Dict[str, Any], key: str, default: str = "") -> str:
    env = profile.get("env") if isinstance(profile.get("env"), dict) else {}
    value = env.get(key)
    if isinstance(value, str) and value and "managed-by-chiploop" not in value and "set-in-secret-manager" not in value:
        return value
    return os.getenv(key, default)


def _route_value(route: Dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def complete_text(
    prompt: str,
    *,
    capability: str = "default",
    agent_name: Optional[str] = None,
    system: str = "",
    state: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
) -> str:
    profile = get_model_profile(state)
    if not agent_name and isinstance(state, dict):
        agent_name = str(state.get("agent_name") or state.get("current_agent") or state.get("step") or "").strip() or None
    route = resolve_route(profile, capability, agent_name=agent_name)
    provider = str(route.get("provider") or profile.get("provider") or "openai").strip().lower()
    messages = _messages(prompt, system)

    if provider == "portkey":
        if Portkey is None:
            raise RuntimeError("portkey-ai is not installed")
        api_key = _env_value(profile, "PORTKEY_API_KEY")
        if not api_key:
            raise RuntimeError("PORTKEY_API_KEY is required for Portkey model profile")
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.5"))
        resp = Portkey(api_key=api_key).chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
            **({"temperature": temperature} if temperature is not None else {}),
        )
        return (resp.choices[0].message.content or "").strip()

    if provider == "azure_openai":
        endpoint = _env_value(profile, "AZURE_OPENAI_ENDPOINT")
        api_key = _env_value(profile, "AZURE_OPENAI_API_KEY")
        api_version = str(profile.get("api_version") or os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))
        deployment = _route_value(route, "deployment", "model", default=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""))
        if not endpoint or not api_key or not deployment:
            raise RuntimeError("Azure OpenAI profile requires endpoint, api key, and deployment")
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        resp = client.chat.completions.create(
            model=deployment,
            messages=messages,
            **({"temperature": temperature} if temperature is not None else {}),
        )
        return (resp.choices[0].message.content or "").strip()

    if provider == "openai_compatible":
        api_key = _env_value(profile, "OPENAI_API_KEY", "not-required")
        base_url = str(profile.get("base_url") or os.getenv("OPENAI_BASE_URL") or "").strip()
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.5"))
        if not base_url:
            raise RuntimeError("OpenAI-compatible profile requires base_url")
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            **({"temperature": temperature} if temperature is not None else {}),
        )
        return (resp.choices[0].message.content or "").strip()

    if provider == "openai":
        api_key = _env_value(profile, "OPENAI_API_KEY")
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.5"))
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            **({"temperature": temperature} if temperature is not None else {}),
        )
        return (resp.choices[0].message.content or "").strip()

    if provider == "anthropic":
        api_key = _env_value(profile, "ANTHROPIC_API_KEY")
        model = _route_value(route, "model", default=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"))
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic model profile")
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": int(route.get("max_tokens") or os.getenv("ANTHROPIC_MAX_TOKENS", "4096")),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        with httpx.Client(timeout=float(route.get("timeout_sec") or 120.0)) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": str(route.get("api_version") or "2023-06-01"),
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        parts = data.get("content") if isinstance(data, dict) else []
        if isinstance(parts, list):
            return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
        return ""

    raise RuntimeError(f"Unsupported ChipLoop model provider: {provider}")


def embed_text(
    text: str,
    *,
    capability: str = "embeddings",
    state: Optional[Dict[str, Any]] = None,
) -> List[float]:
    text = (text or "").strip()
    if not text:
        return []

    profile = get_model_profile(state)
    route = resolve_route(profile, capability)
    provider = str(route.get("provider") or profile.get("provider") or "openai").strip().lower()
    model = _route_value(route, "model", default=os.getenv("CHIPLOOP_EMBEDDING_MODEL", "text-embedding-3-small"))

    if provider == "azure_openai":
        endpoint = _env_value(profile, "AZURE_OPENAI_ENDPOINT")
        api_key = _env_value(profile, "AZURE_OPENAI_API_KEY")
        api_version = str(profile.get("api_version") or os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))
        deployment = _route_value(route, "deployment", "model", default=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", ""))
        if not endpoint or not api_key or not deployment:
            raise RuntimeError("Azure OpenAI embedding profile requires endpoint, api key, and deployment")
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        resp = client.embeddings.create(model=deployment, input=text)
        return list(resp.data[0].embedding)

    if provider == "openai_compatible":
        api_key = _env_value(profile, "OPENAI_API_KEY", "not-required")
        base_url = str(route.get("base_url") or profile.get("base_url") or os.getenv("OPENAI_BASE_URL") or "").strip()
        if not base_url:
            raise RuntimeError("OpenAI-compatible embedding profile requires base_url")
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.embeddings.create(model=model, input=text)
        return list(resp.data[0].embedding)

    if provider == "openai":
        api_key = _env_value(profile, "OPENAI_API_KEY")
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        resp = client.embeddings.create(model=model, input=text)
        return list(resp.data[0].embedding)

    raise RuntimeError(f"Unsupported ChipLoop embedding provider: {provider}")
