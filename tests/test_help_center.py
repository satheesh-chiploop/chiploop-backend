import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_auth
import browser_routes


JWT_SECRET = "help-center-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _client() -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.include_router(browser_routes.router)
    return TestClient(app)


def _auth(user_id: str = "user-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def test_help_ask_answers_from_guide_content():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How do I connect GitHub and import a repo?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "GitHub" in data["answer"]
    assert data["sources"][0]["slug"] == "github"
    assert data["suggested_actions"]


def test_help_ask_rejects_short_question():
    response = _client().post("/help/ask", headers=_auth(), json={"question": "?"})

    assert response.status_code == 400
    assert response.json()["detail"] == "question_too_short"
