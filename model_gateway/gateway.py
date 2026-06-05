import os
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from openai import AzureOpenAI, OpenAI
from openai import APITimeoutError, APIConnectionError, RateLimitError, BadRequestError

try:
    from portkey_ai import Portkey
except Exception:  # pragma: no cover - optional dependency in private installs
    Portkey = None

try:
    import boto3
except Exception:  # pragma: no cover - optional dependency in private installs
    boto3 = None

from .profiles import get_model_profile, model_profile_summary, resolve_route
from .usage import record_model_usage

logger = logging.getLogger("chiploop.model_gateway")


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


def _timeout_value(route: Dict[str, Any]) -> float:
    raw = route.get("timeout_sec") or os.getenv("CHIPLOOP_LLM_TIMEOUT_SEC", "90")
    try:
        return max(5.0, float(raw))
    except Exception:
        return 90.0


def _max_completion_args(route: Dict[str, Any]) -> Dict[str, int]:
    raw = route.get("max_completion_tokens") or route.get("max_tokens") or os.getenv("CHIPLOOP_LLM_MAX_COMPLETION_TOKENS", "")
    if not raw:
        return {}
    try:
        return {"max_completion_tokens": int(raw)}
    except Exception:
        return {}


def _bool_route_value(route: Dict[str, Any], key: str, env_key: str, default: bool = False) -> bool:
    value = route.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    raw = os.getenv(env_key)
    if isinstance(raw, str) and raw:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return default


def _wrap_model_error(provider: str, model: str, exc: Exception) -> RuntimeError:
    message = str(exc)
    if isinstance(exc, RateLimitError) or "429" in message or "too many requests" in message.lower():
        return RuntimeError(
            f"model_rate_limited provider={provider} model={model}: "
            f"{type(exc).__name__}: {message}"
        )
    return RuntimeError(
        f"ChipLoop model call failed provider={provider} model={model}: "
        f"{type(exc).__name__}: {message}"
    )


def _log_call_start(provider: str, model: str, capability: str, agent_name: Optional[str], prompt: str) -> float:
    logger.info(
        "model.call.start provider=%s model=%s capability=%s agent=%s prompt_chars=%s",
        provider,
        model,
        capability,
        agent_name or "",
        len(prompt or ""),
    )
    return time.monotonic()


def _extract_chat_text(resp: Any, provider: str, model: str, started_at: float) -> str:
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    finish_reason = getattr(choice, "finish_reason", "") if choice is not None else ""
    message = getattr(choice, "message", None) if choice is not None else None
    content = getattr(message, "content", "") if message is not None else ""
    text = (content or "").strip()
    logger.info(
        "model.call.complete provider=%s model=%s elapsed_sec=%.2f finish_reason=%s response_chars=%s",
        provider,
        model,
        time.monotonic() - started_at,
        finish_reason,
        len(text),
    )
    if not text:
        raise RuntimeError(f"OpenAI-compatible response was empty provider={provider} model={model} finish_reason={finish_reason}")
    if finish_reason == "length":
        raise RuntimeError(f"Model response was truncated provider={provider} model={model}; increase max_completion_tokens")
    return text


def _chunk_delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return ""
    content = getattr(delta, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(delta, dict):
        value = delta.get("content", "")
        return value if isinstance(value, str) else ""
    return ""


def _chunk_finish_reason(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    return str(getattr(choices[0], "finish_reason", "") or "")


def _collect_stream_text(stream: Any, provider: str, model: str, started_at: float) -> str:
    parts: List[str] = []
    finish_reason = ""
    last_log_at = started_at
    for chunk in stream:
        delta = _chunk_delta_text(chunk)
        if delta:
            parts.append(delta)
        fr = _chunk_finish_reason(chunk)
        if fr:
            finish_reason = fr
        now = time.monotonic()
        if now - last_log_at >= 10.0:
            logger.info(
                "model.call.stream provider=%s model=%s elapsed_sec=%.2f response_chars=%s",
                provider,
                model,
                now - started_at,
                sum(len(p) for p in parts),
            )
            last_log_at = now

    text = "".join(parts).strip()
    logger.info(
        "model.call.complete provider=%s model=%s elapsed_sec=%.2f finish_reason=%s response_chars=%s stream=true",
        provider,
        model,
        time.monotonic() - started_at,
        finish_reason,
        len(text),
    )
    if not text:
        raise RuntimeError(f"Streamed response was empty provider={provider} model={model} finish_reason={finish_reason}")
    if finish_reason == "length":
        raise RuntimeError(f"Streamed model response was truncated provider={provider} model={model}; increase max_completion_tokens")
    return text


def _record_success(
    *,
    state: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    route: Dict[str, Any],
    provider: str,
    model: str,
    capability: str,
    agent_name: Optional[str],
    prompt: str,
    output: str,
    provider_usage: Any = None,
    request_type: str = "chat",
    stream: bool = False,
    started_at: Optional[float] = None,
) -> None:
    record_model_usage(
        state=state,
        profile=profile,
        route=route,
        provider=provider,
        model=model,
        capability=capability,
        agent_name=agent_name,
        prompt=prompt,
        output=output,
        provider_usage=provider_usage,
        request_type=request_type,
        stream=stream,
        status="ok",
        started_at=started_at,
    )


def _record_failure(
    *,
    state: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    route: Dict[str, Any],
    provider: str,
    model: str,
    capability: str,
    agent_name: Optional[str],
    prompt: str,
    exc: Exception,
    request_type: str = "chat",
    stream: bool = False,
    started_at: Optional[float] = None,
) -> None:
    record_model_usage(
        state=state,
        profile=profile,
        route=route,
        provider=provider,
        model=model,
        capability=capability,
        agent_name=agent_name,
        prompt=prompt,
        output="",
        request_type=request_type,
        stream=stream,
        status="failed",
        error_type=type(exc).__name__,
        started_at=started_at,
    )


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
    stream = _bool_route_value(route, "stream", "CHIPLOOP_LLM_STREAM", default=True)

    if provider == "portkey":
        if Portkey is None:
            raise RuntimeError("portkey-ai is not installed")
        api_key = _env_value(profile, "PORTKEY_API_KEY")
        if not api_key:
            raise RuntimeError("PORTKEY_API_KEY is required for Portkey model profile")
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.4-mini"))
        started_at = _log_call_start(provider, model, capability, agent_name, prompt)
        try:
            resp = Portkey(api_key=api_key).chat.completions.create(
                model=model,
                messages=messages,
                stream=stream,
                **_max_completion_args(route),
                **({"temperature": temperature} if temperature is not None else {}),
            )
        except Exception as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, stream=stream, started_at=started_at)
            raise _wrap_model_error(provider, model, exc) from exc
        if stream:
            text = _collect_stream_text(resp, provider, model, started_at)
            _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, stream=True, started_at=started_at)
            return text
        text = _extract_chat_text(resp, provider, model, started_at)
        _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=getattr(resp, "usage", None), started_at=started_at)
        return text

    if provider == "azure_openai":
        endpoint = _env_value(profile, "AZURE_OPENAI_ENDPOINT")
        api_key = _env_value(profile, "AZURE_OPENAI_API_KEY")
        api_version = str(profile.get("api_version") or os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))
        deployment = _route_value(route, "deployment", "model", default=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""))
        if not endpoint or not api_key or not deployment:
            raise RuntimeError("Azure OpenAI profile requires endpoint, api key, and deployment")
        started_at = _log_call_start(provider, deployment, capability, agent_name, prompt)
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=_timeout_value(route),
            max_retries=int(route.get("max_retries") or os.getenv("CHIPLOOP_LLM_MAX_RETRIES", "1")),
        )
        try:
            resp = client.chat.completions.create(
                model=deployment,
                messages=messages,
                stream=stream,
                **_max_completion_args(route),
                **({"temperature": temperature} if temperature is not None else {}),
            )
        except Exception as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=deployment, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, stream=stream, started_at=started_at)
            raise _wrap_model_error(provider, deployment, exc) from exc
        if stream:
            text = _collect_stream_text(resp, provider, deployment, started_at)
            _record_success(state=state, profile=profile, route=route, provider=provider, model=deployment, capability=capability, agent_name=agent_name, prompt=prompt, output=text, stream=True, started_at=started_at)
            return text
        text = _extract_chat_text(resp, provider, deployment, started_at)
        _record_success(state=state, profile=profile, route=route, provider=provider, model=deployment, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=getattr(resp, "usage", None), started_at=started_at)
        return text

    if provider == "openai_compatible":
        api_key = _env_value(profile, "OPENAI_API_KEY", "not-required")
        base_url = str(profile.get("base_url") or os.getenv("OPENAI_BASE_URL") or "").strip()
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.4-mini"))
        if not base_url:
            raise RuntimeError("OpenAI-compatible profile requires base_url")
        started_at = _log_call_start(provider, model, capability, agent_name, prompt)
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=_timeout_value(route),
            max_retries=int(route.get("max_retries") or os.getenv("CHIPLOOP_LLM_MAX_RETRIES", "1")),
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=stream,
                **_max_completion_args(route),
                **({"temperature": temperature} if temperature is not None else {}),
            )
        except Exception as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, stream=stream, started_at=started_at)
            raise _wrap_model_error(provider, model, exc) from exc
        if stream:
            text = _collect_stream_text(resp, provider, model, started_at)
            _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, stream=True, started_at=started_at)
            return text
        text = _extract_chat_text(resp, provider, model, started_at)
        _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=getattr(resp, "usage", None), started_at=started_at)
        return text

    if provider == "openai":
        api_key = _env_value(profile, "OPENAI_API_KEY")
        model = _route_value(route, "model", default=os.getenv("CHIPLOOP_DEFAULT_MODEL", "gpt-5.4-mini"))
        client_kwargs = {
            "timeout": _timeout_value(route),
            "max_retries": int(route.get("max_retries") or os.getenv("CHIPLOOP_LLM_MAX_RETRIES", "1")),
        }
        client = OpenAI(api_key=api_key, **client_kwargs) if api_key else OpenAI(**client_kwargs)
        started_at = _log_call_start(provider, model, capability, agent_name, prompt)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=stream,
                **_max_completion_args(route),
                **({"temperature": temperature} if temperature is not None else {}),
            )
        except (APITimeoutError, APIConnectionError, RateLimitError, BadRequestError) as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, stream=stream, started_at=started_at)
            raise _wrap_model_error(provider, model, exc) from exc
        except Exception as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, stream=stream, started_at=started_at)
            raise _wrap_model_error(provider, model, exc) from exc
        if stream:
            text = _collect_stream_text(resp, provider, model, started_at)
            _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, stream=True, started_at=started_at)
            return text
        text = _extract_chat_text(resp, provider, model, started_at)
        _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=getattr(resp, "usage", None), started_at=started_at)
        return text

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
            started_at = _log_call_start(provider, model, capability, agent_name, prompt)
            try:
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
            except Exception as exc:
                _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, started_at=started_at)
                raise
        parts = data.get("content") if isinstance(data, dict) else []
        if isinstance(parts, list):
            text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
            _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=data.get("usage") if isinstance(data, dict) else None, started_at=started_at)
            return text
        _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output="", provider_usage=data.get("usage") if isinstance(data, dict) else None, started_at=started_at)
        return ""

    if provider == "aws_bedrock":
        if boto3 is None:
            raise RuntimeError("boto3 is required for AWS Bedrock model profiles")
        model = _route_value(route, "model", default=os.getenv("AWS_BEDROCK_MODEL_ID", ""))
        region = _route_value(route, "region", default=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")))
        if not model:
            raise RuntimeError("AWS Bedrock profile requires a model id")
        inference: Dict[str, Any] = {}
        max_tokens = route.get("max_completion_tokens") or route.get("max_tokens")
        if max_tokens:
            inference["maxTokens"] = int(max_tokens)
        if temperature is not None:
            inference["temperature"] = float(temperature)
        payload: Dict[str, Any] = {
            "modelId": model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
        }
        if system:
            payload["system"] = [{"text": system}]
        if inference:
            payload["inferenceConfig"] = inference
        started_at = _log_call_start(provider, model, capability, agent_name, prompt)
        try:
            response = boto3.client("bedrock-runtime", region_name=region).converse(**payload)
        except Exception as exc:
            _record_failure(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, exc=exc, started_at=started_at)
            raise _wrap_model_error(provider, model, exc) from exc
        content = (((response or {}).get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
        logger.info(
            "model.call.complete provider=%s model=%s elapsed_sec=%.2f response_chars=%s",
            provider,
            model,
            time.monotonic() - started_at,
            len(text),
        )
        if not text:
            raise RuntimeError(f"AWS Bedrock response was empty provider={provider} model={model}")
        _record_success(state=state, profile=profile, route=route, provider=provider, model=model, capability=capability, agent_name=agent_name, prompt=prompt, output=text, provider_usage=(response or {}).get("usage"), started_at=started_at)
        return text

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
