import sys
from pathlib import Path

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from onboarding import InMemoryOnboardingRepository, OnboardingService


JWT_SECRET = "onboarding-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _client(service: OnboardingService | None = None) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.onboarding_service = service or OnboardingService(InMemoryOnboardingRepository())
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_onboarding_requires_session():
    response = _client().get("/settings/onboarding")
    assert response.status_code == 401


def test_onboarding_defaults_to_incomplete():
    response = _client().get("/settings/onboarding", headers=_auth())
    assert response.status_code == 200
    onboarding = response.json()["onboarding"]
    assert onboarding["completed"] is False
    assert onboarding["completed_at"] is None


def test_onboarding_complete_records_workflow_id():
    client = _client()
    response = client.post(
        "/settings/onboarding",
        headers=_auth("user-1"),
        json={"action": "complete", "workflow_id": "wf-123", "last_step": "downloaded"},
    )
    assert response.status_code == 200
    onboarding = response.json()["onboarding"]
    assert onboarding["completed"] is True
    assert onboarding["first_workflow_id"] == "wf-123"
    assert onboarding["last_step"] == "downloaded"


def test_onboarding_is_user_scoped():
    service = OnboardingService(InMemoryOnboardingRepository())
    client = _client(service)
    client.post("/settings/onboarding", headers=_auth("user-1"), json={"action": "skip"})

    user_1 = client.get("/settings/onboarding", headers=_auth("user-1")).json()["onboarding"]
    user_2 = client.get("/settings/onboarding", headers=_auth("user-2")).json()["onboarding"]

    assert user_1["completed"] is True
    assert user_2["completed"] is False
