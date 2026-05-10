import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_auth
import browser_routes


JWT_SECRET = "github-test-secret"


def _auth() -> dict:
    token = jwt.encode({"sub": "user-1", "email": "user@example.com"}, JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


class FakeGitHubService:
    def __init__(self):
        self.installation = None

    def status(self, user_id=None):
        return {
            "configured": True,
            "connected": bool(self.installation),
            "auth_mode": "test",
            "connect_url": "https://github.com/apps/chiploop/installations/new",
            "installation": self.installation,
            "message": None,
        }

    async def register_installation(self, user_id, installation_id):
        self.installation = {
            "user_id": user_id,
            "github_installation_id": installation_id,
            "github_account_login": "chiploop",
            "status": "active",
        }
        return self.installation

    def disconnect(self, user_id, installation_id):
        if self.installation and self.installation["user_id"] == user_id:
            self.installation = None
            return True
        return False

    async def list_repositories(self, user_id=None):
        return [{"full_name": "chiploop/demo", "default_branch": "main", "private": False}]

    async def list_tree(self, owner, repo, ref="main", path="", user_id=None):
        return [{"name": "top.sv", "path": "rtl/top.sv", "type": "file", "size": 12, "sha": "abc"}]

    async def read_files(self, owner, repo, paths, ref="main", user_id=None):
        return [{"path": paths[0], "name": "top.sv", "sha": "abc", "size": 12, "content": "module top; endmodule"}]


def _client() -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.github_service = FakeGitHubService()
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_github_status_requires_session_and_returns_config():
    client = _client()

    assert client.get("/settings/github/status").status_code == 401
    response = client.get("/settings/github/status", headers=_auth())

    assert response.status_code == 200
    assert response.json()["github"]["configured"] is True


def test_github_import_returns_files_and_combined_text():
    client = _client()

    response = client.post(
        "/github/import",
        headers=_auth(),
        json={"owner": "chiploop", "repo": "demo", "ref": "main", "paths": ["rtl/top.sv"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["files"][0]["path"] == "rtl/top.sv"
    assert "module top" in body["combined_text"]


def test_github_callback_stores_user_installation():
    client = _client()

    response = client.post(
        "/settings/github/callback",
        headers=_auth(),
        json={"installation_id": "12345"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["installation"]["github_installation_id"] == "12345"
    assert body["github"]["connected"] is True


def test_github_export_plan_contract():
    client = _client()

    response = client.post(
        "/github/export-plan",
        headers=_auth(),
        json={"workflow_id": "wf-1234567890", "owner": "chiploop", "repo": "demo"},
    )

    assert response.status_code == 200
    plan = response.json()["export_plan"]
    assert plan["branch"] == "chiploop/wf-12345"
    assert plan["next_steps"]
