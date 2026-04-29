import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_auth
import browser_routes
from user_agents.repository import UserAgentRepository
from user_agents.service import UserAgentService, build_effective_agent_catalog


JWT_SECRET = "phase7h-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


class FakeUserAgentRepository(UserAgentRepository):
    def __init__(self):
        self.rows = {}
        self.submissions = []
        self.next_id = 1

    def list_by_owner(self, user_id):
        return [row for row in self.rows.values() if row.get("owner_id") == user_id and row.get("is_custom")]

    def list_global_visible(self):
        return [
            row
            for row in self.rows.values()
            if row.get("visibility") in {"global", "marketplace"}
            or row.get("status") == "approved"
            or row.get("is_prebuilt")
        ]

    def insert_private(self, row):
        item = {"id": f"agent-{self.next_id}", "created_at": "2026-01-01T00:00:00Z", **row}
        self.next_id += 1
        self.rows[item["id"]] = item
        return item

    def get_owned(self, user_id, agent_id):
        row = self.rows.get(agent_id)
        if not row or row.get("owner_id") != user_id or not row.get("is_custom"):
            return None
        return row

    def delete_owned(self, user_id, agent_id):
        if not self.get_owned(user_id, agent_id):
            return False
        del self.rows[agent_id]
        return True

    def update_owned(self, user_id, agent_id, patch):
        row = self.get_owned(user_id, agent_id)
        if not row:
            return None
        row.update(patch)
        return row

    def create_marketplace_submission(self, row):
        self.submissions.append(row)
        return {"id": f"submission-{len(self.submissions)}", **row}


def _client(service: UserAgentService) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.user_agent_service = service
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_private_agent_save_stores_owner_and_private_visibility():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)

    saved = service.save_private_agent(
        "user-1",
        {
            "name": "My STA Agent",
            "loop_type": "system",
            "description": "Create STA constraints",
            "agent_spec": {"name": "My STA Agent", "required_skills": ["sta_constraint_generation"]},
        },
    )

    assert saved["owner_id"] == "user-1"
    assert saved["visibility"] == "private"
    assert saved["status"] == "private"
    assert saved["is_custom"] is True
    assert saved["is_prebuilt"] is False
    assert saved["agent_spec"]["name"] == "My STA Agent"


def test_list_returns_only_current_users_private_agents():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    mine = service.save_private_agent("user-1", {"name": "Mine Agent"})
    service.save_private_agent("user-2", {"name": "Other Agent"})

    listed = service.list_my_agents("user-1")

    assert [item["id"] for item in listed] == [mine["id"]]
    assert listed[0]["agent_name"] == "Mine Agent"


def test_delete_enforces_ownership():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    other = service.save_private_agent("user-2", {"name": "Other Agent"})

    assert service.delete_my_agent("user-1", other["id"]) is False
    assert service.delete_my_agent("user-2", other["id"]) is True


def test_submit_does_not_make_agent_global():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    saved = service.save_private_agent("user-1", {"name": "Submit Me"})

    result = service.submit_my_agent("user-1", saved["id"])

    assert result["ok"] is True
    assert result["agent"]["status"] == "submitted"
    assert result["agent"]["visibility"] == "private"
    assert result["marketplace_submission_created"] is True
    assert repo.submissions[0]["status"] == "pending"


def test_effective_catalog_includes_platform_and_my_private_agents():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    service.save_private_agent("user-1", {"name": "Private STA Agent", "loop_type": "system"})

    catalog = build_effective_agent_catalog(
        repo,
        "user-1",
        platform_agents={"Digital RTL Agent": {"description": "platform"}},
    )

    assert "Digital RTL Agent" in catalog
    assert catalog["Digital RTL Agent"]["source"] == "platform"
    assert "Private STA Agent" in catalog
    assert catalog["Private STA Agent"]["source"] == "private"


def test_effective_catalog_excludes_other_users_private_agents():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    service.save_private_agent("user-2", {"name": "Other Private Agent"})

    catalog = build_effective_agent_catalog(repo, "user-1", platform_agents={})

    assert "Other Private Agent" not in catalog


def test_browser_user_agent_endpoints_enforce_session_and_ownership():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    client = _client(service)

    unauth = client.get("/studio/user-agents")
    assert unauth.status_code == 401

    create = client.post("/studio/user-agents", headers=_auth("user-1"), json={"name": "Browser Agent"})
    assert create.status_code == 200
    agent_id = create.json()["agent"]["id"]

    other_delete = client.delete(f"/studio/user-agents/{agent_id}", headers=_auth("user-2"))
    assert other_delete.status_code == 404

    listed = client.get("/studio/user-agents", headers=_auth("user-1"))
    assert listed.status_code == 200
    assert listed.json()["agents"][0]["agent_name"] == "Browser Agent"


def test_browser_submit_endpoint_keeps_visibility_private():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    saved = service.save_private_agent("user-1", {"name": "Submit Browser Agent"})
    client = _client(service)

    response = client.post(f"/studio/user-agents/{saved['id']}/submit", headers=_auth("user-1"))

    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["status"] == "submitted"
    assert body["agent"]["visibility"] == "private"
