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
    def status(self):
        return {"configured": True, "auth_mode": "test", "connect_url": None, "message": None}

    async def list_repositories(self):
        return [{"full_name": "chiploop/demo", "default_branch": "main", "private": False}]

    async def list_tree(self, owner, repo, ref="main", path=""):
        return [{"name": "top.sv", "path": "rtl/top.sv", "type": "file", "size": 12, "sha": "abc"}]

    async def read_files(self, owner, repo, paths, ref="main"):
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
