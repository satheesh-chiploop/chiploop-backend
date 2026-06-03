from model_gateway.gateway import _wrap_model_error


def test_rate_limit_error_is_classified():
    error = _wrap_model_error("openai", "gpt-5.5", RuntimeError("429 Too Many Requests"))
    assert str(error).startswith("model_rate_limited provider=openai model=gpt-5.5")
