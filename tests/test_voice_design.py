import sys
from pathlib import Path

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from billing import BillingService, InMemoryBillingRepository


JWT_SECRET = "voice-design-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _client(plan_id: str = "pro") -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.billing_service = BillingService(InMemoryBillingRepository(default_plan_id=plan_id))
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_voice_transcribe_requires_checkout():
    client = _client("account")

    response = client.post(
        "/studio/voice/transcribe",
        headers=_auth(),
        files={"file": ("voice.webm", b"audio", "audio/webm")},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "trial_checkout_required"


def test_voice_transcribe_returns_transcript(monkeypatch):
    monkeypatch.setattr(browser_routes, "transcribe_audio_bytes", lambda *_args, **_kwargs: "Build a PWM block.")
    client = _client("pro")

    response = client.post(
        "/studio/voice/transcribe",
        headers=_auth(),
        files={"file": ("voice.webm", b"audio", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json()["transcript"] == "Build a PWM block."


def test_voice_spec_draft_returns_structured_spec(monkeypatch):
    async def fake_draft(transcripts, **_kwargs):
        return "Goal: Build a PWM controller\nInterfaces: clk, reset_n"

    monkeypatch.setattr(browser_routes, "build_spec_draft", fake_draft)
    client = _client("pro")

    response = client.post(
        "/studio/voice/spec-draft",
        headers=_auth(),
        json={"transcripts": ["Build PWM"], "loop_type": "digital", "target": "Arch2RTL"},
    )

    assert response.status_code == 200
    assert "PWM controller" in response.json()["spec_draft"]
