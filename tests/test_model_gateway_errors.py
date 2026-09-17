from types import SimpleNamespace

import pytest

from model_gateway import gateway
from model_gateway.gateway import _model_candidates, _wrap_model_error


def test_rate_limit_error_is_classified():
    error = _wrap_model_error("openai", "gpt-5.5", RuntimeError("429 Too Many Requests"))
    assert str(error).startswith("model_rate_limited provider=openai model=gpt-5.5")


def test_model_candidates_are_ordered_and_deduplicated(monkeypatch):
    monkeypatch.delenv("CHIPLOOP_MODEL_NOT_FOUND_FALLBACKS", raising=False)
    assert _model_candidates(
        {"fallback_models": ["gpt-5-mini", "gpt-5.4-mini"]}, "gpt-5.4-mini"
    ) == ["gpt-5.4-mini", "gpt-5-mini"]


def test_openai_model_not_found_uses_configured_fallback(monkeypatch):
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "gpt-5.4-mini":
                raise RuntimeError("Error code: 404 model not found")
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="fallback worked"),
                )],
                usage=None,
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(gateway, "OpenAI", lambda **kwargs: fake_client)
    monkeypatch.setattr(gateway, "_record_failure", lambda **kwargs: None)
    monkeypatch.setattr(gateway, "_record_success", lambda **kwargs: None)

    text = gateway.complete_text(
        "prompt",
        state={"model_profile": {
            "provider": "openai",
            "routing": {"default": {
                "model": "gpt-5.4-mini",
                "fallback_models": ["gpt-5-mini"],
                "stream": False,
            }},
        }},
    )

    assert text == "fallback worked"
    assert calls == ["gpt-5.4-mini", "gpt-5-mini"]


def test_openai_non_availability_error_does_not_fallback(monkeypatch):
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs["model"])
            raise RuntimeError("401 invalid api key")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(gateway, "OpenAI", lambda **kwargs: fake_client)
    monkeypatch.setattr(gateway, "_record_failure", lambda **kwargs: None)

    with pytest.raises(RuntimeError, match="401 invalid api key"):
        gateway.complete_text(
            "prompt",
            state={"model_profile": {
                "provider": "openai",
                "routing": {"default": {
                    "model": "gpt-5.4-mini",
                    "fallback_models": ["gpt-5-mini"],
                    "stream": False,
                }},
            }},
        )

    assert calls == ["gpt-5.4-mini"]
