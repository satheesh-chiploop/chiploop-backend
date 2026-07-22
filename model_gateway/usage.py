import math
import os
import time
import logging
import json
from typing import Any, Dict, Optional

from platform_adapters import get_platform_client

logger = logging.getLogger("chiploop.model_gateway.usage")
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET_NAME", "artifacts")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _usage_value(usage: Any, *names: str) -> Optional[int]:
    if usage is None:
        return None
    for name in names:
        if isinstance(usage, dict) and name in usage:
            return _int_or_none(usage.get(name))
        value = getattr(usage, name, None)
        if value is not None:
            return _int_or_none(value)
    return None


def estimate_tokens(text: str) -> int:
    text = text or ""
    if not text:
        return 0
    # Conservative fallback when provider usage is unavailable, especially for streams.
    return max(1, math.ceil(len(text) / 4))


def usage_counts(prompt: str, output: str, provider_usage: Any = None) -> Dict[str, int]:
    input_tokens = _usage_value(provider_usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(provider_usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(provider_usage, "total_tokens")
    if input_tokens is None:
        input_tokens = estimate_tokens(prompt)
    if output_tokens is None:
        output_tokens = estimate_tokens(output)
    if total_tokens is None:
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or 0),
    }


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _estimated_cost_usd(route: Dict[str, Any], counts: Dict[str, int]) -> Optional[float]:
    input_rate = _float_or_none(route.get("input_cost_per_1m_tokens") or os.getenv("CHIPLOOP_MODEL_INPUT_COST_PER_1M"))
    output_rate = _float_or_none(route.get("output_cost_per_1m_tokens") or os.getenv("CHIPLOOP_MODEL_OUTPUT_COST_PER_1M"))
    if input_rate is None and output_rate is None:
        return None
    input_cost = (counts.get("input_tokens", 0) / 1_000_000.0) * float(input_rate or 0)
    output_cost = (counts.get("output_tokens", 0) / 1_000_000.0) * float(output_rate or 0)
    return round(input_cost + output_cost, 6)


def _estimated_credits(route: Dict[str, Any], counts: Dict[str, int], cost_usd: Optional[float]) -> int:
    if cost_usd is not None:
        credits_per_usd = _float_or_none(route.get("credits_per_usd") or os.getenv("CHIPLOOP_AI_CREDITS_PER_USD"))
        if credits_per_usd:
            return max(1, math.ceil(float(cost_usd) * credits_per_usd))
    rate = _float_or_none(route.get("credits_per_1k_tokens") or os.getenv("CHIPLOOP_AI_CREDITS_PER_1K_TOKENS") or "1")
    return max(1, math.ceil((counts.get("total_tokens", 0) / 1000.0) * float(rate or 1)))


def _context_value(state: Optional[Dict[str, Any]], *names: str) -> Optional[str]:
    if not isinstance(state, dict):
        return None
    for name in names:
        value = state.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def ai_billing_mode(profile: Dict[str, Any], state: Optional[Dict[str, Any]]) -> str:
    raw = (
        _context_value(state, "ai_billing_mode")
        or str(profile.get("ai_billing_mode") or "")
        or os.getenv("CHIPLOOP_AI_BILLING_MODE", "")
        or "byok"
    )
    return "chiploop_managed" if raw.strip().lower() in {"managed", "chiploop", "chiploop_managed"} else "byok"


def model_key_owner(profile: Dict[str, Any], state: Optional[Dict[str, Any]], mode: str) -> str:
    raw = (
        _context_value(state, "model_key_owner")
        or str(profile.get("model_key_owner") or "")
        or os.getenv("CHIPLOOP_MODEL_KEY_OWNER", "")
    )
    if raw.strip().lower() in {"chiploop", "managed", "chiploop_managed"}:
        return "chiploop"
    if raw.strip().lower() == "customer":
        return "customer"
    return "chiploop" if mode == "chiploop_managed" else "customer"


def _append_usage_artifact(row: Dict[str, Any]) -> None:
    workflow_id = row.get("workflow_id")
    if not workflow_id:
        return
    storage_path = f"backend/workflows/{workflow_id}/metrics/model_usage_events.jsonl"
    try:
        client = get_platform_client()
        bucket = client.storage.from_(ARTIFACT_BUCKET)
        try:
            existing_raw = bucket.download(storage_path)
            existing = existing_raw.decode("utf-8", errors="replace") if isinstance(existing_raw, (bytes, bytearray)) else str(existing_raw or "")
        except Exception:
            existing = ""
        line = json.dumps(row, default=str, separators=(",", ":"))
        content = (existing.rstrip("\n") + "\n" + line + "\n") if existing.strip() else (line + "\n")
        try:
            bucket.upload(storage_path, content.encode("utf-8"), {"content-type": "application/jsonl"})
        except Exception:
            bucket.update(storage_path, content.encode("utf-8"), {"content-type": "application/jsonl"})
    except Exception as exc:
        logger.warning(
            "model_usage.artifact_failed workflow_id=%s run_id=%s agent=%s error=%s",
            row.get("workflow_id"),
            row.get("run_id"),
            row.get("agent_name"),
            exc,
        )


def record_model_usage(
    *,
    state: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    route: Dict[str, Any],
    provider: str,
    model: str,
    capability: str,
    agent_name: Optional[str],
    prompt: str,
    output: str = "",
    provider_usage: Any = None,
    request_type: str = "chat",
    stream: bool = False,
    status: str = "ok",
    error_type: Optional[str] = None,
    started_at: Optional[float] = None,
) -> None:
    counts = usage_counts(prompt, output, provider_usage)
    mode = ai_billing_mode(profile, state)
    owner = model_key_owner(profile, state, mode)
    cost = _estimated_cost_usd(route, counts)
    credits = _estimated_credits(route, counts, cost)
    customer_id = _context_value(state, "customer_id")
    row: Dict[str, Any] = {
        "customer_id": customer_id,
        "org_id": _context_value(state, "org_id", "organization_id", "tenant_id"),
        "user_id": _context_value(state, "user_id"),
        "workflow_id": _context_value(state, "workflow_id"),
        "run_id": _context_value(state, "run_id"),
        "agent_name": agent_name,
        "capability": capability,
        "provider": provider,
        "model": model,
        "ai_billing_mode": mode,
        "model_key_owner": owner,
        "request_type": request_type,
        "stream": bool(stream),
        "input_tokens": counts["input_tokens"],
        "output_tokens": counts["output_tokens"],
        "total_tokens": counts["total_tokens"],
        "input_chars": len(prompt or ""),
        "output_chars": len(output or ""),
        "estimated_cost_usd": cost,
        "estimated_credits": credits,
        "latency_ms": int((time.monotonic() - started_at) * 1000) if started_at else None,
        "status": status,
        "error_type": error_type,
        "metadata": {
            "route_model": route.get("model"),
            "route_provider": route.get("provider"),
            "estimated_tokens": provider_usage is None,
        },
    }
    row = {k: v for k, v in row.items() if v is not None}
    _append_usage_artifact(row)
    try:
        client = get_platform_client()
        result = client.table("model_usage_events").insert(row).execute()
        rows = getattr(result, "data", None) or []
        usage_id = (rows[0] or {}).get("id") if rows else None
        if mode == "chiploop_managed":
            ledger = {
                "customer_id": customer_id,
                "org_id": row.get("org_id"),
                "user_id": row.get("user_id"),
                "workflow_id": row.get("workflow_id"),
                "run_id": row.get("run_id"),
                "model_usage_event_id": usage_id,
                "event_type": "model_usage",
                "credits_delta": -int(credits),
                "metadata": {
                    "provider": provider,
                    "model": model,
                    "agent_name": agent_name,
                    "capability": capability,
                    "total_tokens": counts["total_tokens"],
                    "estimated_cost_usd": cost,
                },
            }
            ledger = {k: v for k, v in ledger.items() if v is not None}
            client.table("org_credit_ledger").insert(ledger).execute()
    except Exception as exc:
        # Usage accounting must not break model execution if a deployment has not
        # applied the billing migration yet.
        logger.warning(
            "model_usage.record_failed workflow_id=%s run_id=%s agent=%s provider=%s model=%s error=%s",
            row.get("workflow_id"),
            row.get("run_id"),
            agent_name,
            provider,
            model,
            exc,
        )
        return
