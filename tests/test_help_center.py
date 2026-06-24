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


def test_help_catalog_lists_registered_agents_and_workflows():
    response = _client().get("/help/catalog", headers=_auth())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["counts"]["agents"] == 197
    assert data["counts"]["workflows"] == 17
    assert data["counts"]["agents_by_loop"]["digital"] == 77
    assert data["counts"]["agents_by_loop"]["system"] == 65
    assert any(row["type"] == "agent" and row["name"] == "Digital Tapeout Logic Equivalence Check Agent" for row in data["rows"])
    assert any(row["type"] == "agent" and row["name"] == "Digital Failure Debug Agent" for row in data["rows"])
    assert any(row["type"] == "agent" and row["name"] == "Digital Spec2RTL Conformance Agent" for row in data["rows"])
    assert any(row["type"] == "workflow" and row["name"] == "Digital_Spec2RTL_Check" for row in data["rows"])
    assert any(row["type"] == "workflow" and row["name"] == "digital_registered_agents" for row in data["rows"])


def test_help_ask_answers_cursor_and_vscode_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How do I use ChipLoop in Cursor or VS Code?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["slug"] == "cursor-vscode"
    assert "CLI" in data["answer"]


def test_help_ask_answers_studio_arrange_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How do I arrange a crowded Studio canvas and understand the arrows?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["slug"] == "studio-workflows"
    assert "arranging" in data["answer"]
    assert "OUT port" in data["answer"]


def test_help_ask_answers_chiploop_differentiation_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How is ChipLoop different from a traditional flow with RTL to product app visibility?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["slug"] == "chiploop-vs-traditional-flow"
    assert "unified" in data["answer"].lower() or "platform" in data["answer"].lower()
    assert "product app" in data["answer"].lower()


def test_help_ask_answers_verification_closure_loop_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How do I run the verification closure loop and chart coverage improvement by iteration?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert any(source["slug"] == "verification-closure-loop" for source in data["sources"])
    assert "coverage" in data["answer"]
    assert "closure" in data["answer"].lower()


def test_help_ask_answers_dft_atpg_macro_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "How do DFT ATPG synthesis LEC tapeout LEC and analog macro collateral work for tapeout?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["slug"] == "digital-implementation-signoff"
    assert "ATPG" in data["answer"]
    assert "tapeout lec" in data["answer"].lower()
    assert "macro" in data["answer"]


def test_help_ask_answers_tapeout_signoff_gating_questions():
    response = _client().post(
        "/help/ask",
        headers=_auth(),
        json={"question": "Why did Arch2Tapeout fail when DRC LVS XOR or GDS signoff is not clean?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["sources"][0]["slug"] == "digital-implementation-signoff"
    assert "DRC" in data["answer"]
    assert "GDS" in data["answer"]
