import sys
from pathlib import Path

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from marketplace import InMemoryMarketplaceRepository, MarketplaceService
from user_agents.repository import UserAgentRepository
from user_agents.service import UserAgentService


JWT_SECRET = "marketplace-test-secret"


class FakeUserAgentRepository(UserAgentRepository):
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def list_by_owner(self, user_id):
        return [row for row in self.rows.values() if row.get("owner_id") == user_id and row.get("is_custom")]

    def list_global_visible(self):
        return []

    def insert_private(self, row):
        item = {"id": f"agent-{self.next_id}", **row}
        self.next_id += 1
        self.rows[item["id"]] = item
        return item

    def get_owned(self, user_id, agent_id):
        row = self.rows.get(agent_id)
        if not row or row.get("owner_id") != user_id:
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
        return row


def _token(user_id: str = "user-1", role: str | None = None) -> str:
    claims = {"sub": user_id}
    if role:
        claims["role"] = role
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1", role: str | None = None) -> dict:
    return {"Authorization": f"Bearer {_token(user_id, role)}"}


def _services():
    user_repo = FakeUserAgentRepository()
    user_service = UserAgentService(user_repo)
    market_repo = InMemoryMarketplaceRepository()
    market_service = MarketplaceService(market_repo, user_service)
    return user_service, market_repo, market_service


def _client(market_service: MarketplaceService) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.marketplace_service = market_service
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_admin_approval_creates_listing_and_version():
    _, repo, service = _services()
    repo.submissions["sub-1"] = {
        "id": "sub-1",
        "agent_id": "agent-1",
        "submitted_by": "user-1",
        "status": "pending",
        "agent_json": {
            "id": "agent-1",
            "owner_id": "user-1",
            "agent_name": "Digital Example Agent",
            "loop_type": "digital",
            "description": "Example marketplace agent",
            "agent_spec": {"name": "Digital Example Agent", "required_tools": ["python"]},
        },
    }

    result = service.approve_submission("sub-1", "admin-1", "looks good")

    assert result["ok"] is True
    assert result["listing"]["status"] == "active"
    assert result["listing"]["slug"] == "digital-example-agent"
    assert repo.versions[0]["version"] == "1.0.0"
    assert repo.submissions["sub-1"]["status"] == "approved"


def test_install_creates_private_workspace_agent():
    _, repo, service = _services()
    listing = repo.create_listing(
        {
            "name": "Installable Agent",
            "slug": "installable-agent",
            "description": "install me",
            "loop_type": "system",
            "agent_spec": {"name": "Installable Agent"},
            "skills": [],
            "tools": [],
            "hooks": [],
            "version": "1.0.0",
            "status": "active",
        }
    )

    result = service.install_agent("user-1", listing["id"])

    assert result["ok"] is True
    assert result["installed_agent"]["owner_id"] == "user-1"
    assert result["installed_agent"]["visibility"] == "private"
    assert result["installed_agent"]["source"] == "marketplace_install"


def test_review_is_clamped_and_user_scoped():
    _, repo, service = _services()
    listing = repo.create_listing({"name": "Rated Agent", "slug": "rated-agent", "status": "active"})

    result = service.review_agent("user-1", listing["id"], 9, "useful")

    assert result["ok"] is True
    assert result["review"]["rating"] == 5
    assert service.list_reviews(listing["id"])[0]["user_id"] == "user-1"


def test_browser_marketplace_and_admin_endpoints():
    _, repo, service = _services()
    listing = repo.create_listing({"name": "Browser Agent", "slug": "browser-agent", "status": "active"})
    repo.submissions["sub-1"] = {"id": "sub-1", "status": "pending", "agent_json": {"agent_name": "Submit Agent"}}
    client = _client(service)

    assert client.get("/marketplace/agents", headers=_auth()).status_code == 200
    install = client.post(f"/marketplace/agents/{listing['id']}/install", headers=_auth("user-1"))
    assert install.status_code == 200

    forbidden = client.get("/admin/marketplace/submissions", headers=_auth("user-1"))
    assert forbidden.status_code == 403

    allowed = client.get("/admin/marketplace/submissions", headers=_auth("admin-1", "admin"))
    assert allowed.status_code == 200
    assert allowed.json()["submissions"][0]["id"] == "sub-1"
