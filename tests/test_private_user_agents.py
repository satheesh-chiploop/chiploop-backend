import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_auth
import browser_routes
from billing import BillingService, InMemoryBillingRepository
from user_agents.repository import UserAgentRepository
from user_agents.service import UserAgentService, build_effective_agent_catalog


JWT_SECRET = "phase7h-test-secret"


def _token(user_id: str = "user-1", *, email: str | None = None) -> str:
    claims = {"sub": user_id}
    if email:
        claims["email"] = email
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1", *, email: str | None = None) -> dict:
    return {"Authorization": f"Bearer {_token(user_id, email=email)}"}


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


def _client(service: UserAgentService, *, default_plan_id: str = "pro", supabase=None) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.user_agent_service = service
    if supabase is not None:
        app.state.supabase = supabase
    app.state.billing_service = BillingService(InMemoryBillingRepository(default_plan_id=default_plan_id))
    app.include_router(browser_routes.router)
    return TestClient(app)


class FakeSupabaseQuery:
    def __init__(self, table):
        self.table_ref = table
        self.filters = []
        self.insert_row = None
        self.update_patch = None
        self.delete_requested = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def insert(self, row):
        self.insert_row = row
        return self

    def update(self, patch):
        self.update_patch = patch
        return self

    def delete(self):
        self.delete_requested = True
        return self

    def execute(self):
        if self.insert_row is not None:
            row = {"id": f"{self.table_ref.name}-{len(self.table_ref.rows) + 1}", "created_at": "2026-01-01T00:00:00Z", **self.insert_row}
            self.table_ref.rows.append(row)
            return type("Result", (), {"data": [row]})()
        matches = [
            row
            for row in self.table_ref.rows
            if all(row.get(key) == value for key, value in self.filters)
        ]
        if self.update_patch is not None:
            for row in matches:
                row.update(self.update_patch)
            return type("Result", (), {"data": matches})()
        if self.delete_requested:
            for row in matches:
                self.table_ref.rows.remove(row)
            return type("Result", (), {"data": matches})()
        return type("Result", (), {"data": matches})()


class FakeSupabaseTable:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows

    def select(self, *args, **kwargs):
        return FakeSupabaseQuery(self).select(*args, **kwargs)

    def insert(self, row):
        return FakeSupabaseQuery(self).insert(row)

    def update(self, patch):
        return FakeSupabaseQuery(self).update(patch)

    def delete(self):
        return FakeSupabaseQuery(self).delete()


class FakeSupabase:
    def __init__(self):
        self.tables = {
            "workflows": [],
            "user_apps": [],
            "marketplace_submissions": [],
        }

    def table(self, name):
        return FakeSupabaseTable(name, self.tables.setdefault(name, []))


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


def test_update_private_agent_keeps_owner_and_private_visibility():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    saved = service.save_private_agent("user-1", {"name": "Editable Agent"})

    updated = service.update_private_agent(
        "user-1",
        saved["id"],
        {
            "name": "Editable Agent",
            "description": "Updated description",
            "visibility": "global",
            "status": "approved",
        },
    )

    assert updated is not None
    assert updated["description"] == "Updated description"
    assert updated["visibility"] == "private"
    assert updated["status"] == "private"
    assert service.update_private_agent("user-2", saved["id"], {"name": "Nope"}) is None


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

    update = client.patch(
        f"/studio/user-agents/{agent_id}",
        headers=_auth("user-1"),
        json={"name": "Browser Agent", "description": "Updated in browser"},
    )
    assert update.status_code == 200
    assert update.json()["agent"]["description"] == "Updated in browser"


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


def test_admin_email_bypasses_private_agent_plan_requirement():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    client = _client(service, default_plan_id="account")

    response = client.post(
        "/studio/user-agents",
        headers=_auth("admin-user", email="chiploop.agx@gmail.com"),
        json={"name": "Admin Private Agent"},
    )

    assert response.status_code == 200
    assert response.json()["agent"]["agent_name"] == "Admin Private Agent"


def test_admin_email_gets_admin_plan_summary_without_checkout():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    client = _client(service, default_plan_id="account")

    response = client.get("/settings/plan", headers=_auth("admin-user", email="chiploop.agx@gmail.com"))

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["is_admin"] is True
    assert plan["requires_checkout"] is False
    assert plan["can_run_workflows"] is True


def test_create_user_app_from_owned_workflow_stays_private():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["workflows"].append(
        {
            "id": "workflow-1",
            "name": "PWM Reference Journey",
            "user_id": "user-1",
            "loop_type": "digital",
            "definitions": {"nodes": [], "edges": []},
        }
    )
    client = _client(service, supabase=supabase)

    response = client.post(
        "/studio/user-apps",
        headers=_auth("user-1"),
        json={
            "workflow_id": "workflow-1",
            "name": "PWM App",
            "description": "Run PWM workflow as an app.",
            "category": "digital",
            "input_schema": {"fields": [{"key": "frequency"}]},
            "default_config": {"frequency": "1kHz"},
        },
    )

    assert response.status_code == 200
    app = response.json()["app"]
    assert app["owner_id"] == "user-1"
    assert app["workflow_id"] == "workflow-1"
    assert app["visibility"] == "private"
    assert app["status"] == "private"
    assert app["marketplace_status"] == "draft"

    listed = client.get("/studio/user-apps", headers=_auth("user-1"))
    assert listed.status_code == 200
    assert listed.json()["apps"][0]["name"] == "PWM App"


def test_create_user_app_derives_contract_from_workflow_settings_when_payload_empty():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["workflows"].append(
        {
            "id": "workflow-contract",
            "name": "PWM Contract Workflow",
            "user_id": "user-1",
            "loop_type": "digital",
            "definitions": {
                "nodes": [],
                "edges": [],
                "workflow_config_schema": {
                    "version": 1,
                    "fields": [{"key": "target_frequency_mhz", "label": "Target frequency", "type": "number"}],
                },
                "default_run_config": {"target_frequency_mhz": 100},
            },
        }
    )
    client = _client(service, supabase=supabase)

    response = client.post(
        "/studio/user-apps",
        headers=_auth("user-1"),
        json={"workflow_id": "workflow-contract", "name": "PWM Contract App"},
    )

    assert response.status_code == 200
    app = response.json()["app"]
    assert app["input_schema"]["fields"][0]["key"] == "target_frequency_mhz"
    assert app["default_config"]["target_frequency_mhz"] == 100


def test_refresh_user_app_contract_from_backing_workflow():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["workflows"].append(
        {
            "id": "workflow-contract",
            "name": "PWM Contract Workflow",
            "user_id": "user-1",
            "loop_type": "digital",
            "definitions": {
                "nodes": [],
                "edges": [],
                "workflow_config_schema": {
                    "version": 1,
                    "fields": [{"key": "duty_cycle", "label": "Duty cycle", "type": "text"}],
                },
                "default_run_config": {"duty_cycle": "50%"},
            },
        }
    )
    supabase.tables["user_apps"].append(
        {
            "id": "app-1",
            "owner_id": "user-1",
            "workflow_id": "workflow-contract",
            "name": "PWM App",
            "slug": "pwm-app",
            "input_schema": {},
            "default_config": {},
            "visibility": "private",
            "status": "private",
            "marketplace_status": "draft",
        }
    )
    client = _client(service, supabase=supabase)

    response = client.patch("/studio/user-apps/app-1", headers=_auth("user-1"), json={"refresh_contract": True})

    assert response.status_code == 200
    app = response.json()["app"]
    assert app["input_schema"]["fields"][0]["key"] == "duty_cycle"
    assert app["default_config"]["duty_cycle"] == "50%"


def test_create_user_app_rejects_other_users_workflow():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["workflows"].append(
        {
            "id": "workflow-2",
            "name": "Other Workflow",
            "user_id": "user-2",
            "loop_type": "system",
            "definitions": {},
        }
    )
    client = _client(service, supabase=supabase)

    response = client.post(
        "/studio/user-apps",
        headers=_auth("user-1"),
        json={"workflow_id": "workflow-2", "name": "Not Mine"},
    )

    assert response.status_code == 403


def test_submit_user_app_keeps_visibility_private_and_creates_submission():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["user_apps"].append(
        {
            "id": "app-1",
            "owner_id": "user-1",
            "workflow_id": "workflow-1",
            "workflow_name": "PWM Reference Journey",
            "name": "PWM App",
            "slug": "pwm-app",
            "visibility": "private",
            "status": "private",
            "marketplace_status": "draft",
        }
    )
    client = _client(service, supabase=supabase)

    response = client.post("/studio/user-apps/app-1/submit", headers=_auth("user-1"))

    assert response.status_code == 200
    body = response.json()
    assert body["app"]["visibility"] == "private"
    assert body["app"]["status"] == "submitted"
    assert body["app"]["marketplace_status"] == "pending"
    assert body["marketplace_submission_created"] is True
    assert supabase.tables["marketplace_submissions"][0]["workflow_json"]["id"] == "app-1"


def test_update_and_delete_user_app_enforces_ownership():
    repo = FakeUserAgentRepository()
    service = UserAgentService(repo)
    supabase = FakeSupabase()
    supabase.tables["user_apps"].append(
        {
            "id": "app-1",
            "owner_id": "user-1",
            "workflow_id": "workflow-1",
            "name": "Old App",
            "slug": "old-app",
            "description": "Old",
            "visibility": "private",
            "status": "private",
            "marketplace_status": "draft",
        }
    )
    client = _client(service, supabase=supabase)

    forbidden = client.patch("/studio/user-apps/app-1", headers=_auth("user-2"), json={"name": "Nope"})
    assert forbidden.status_code == 404

    update = client.patch(
        "/studio/user-apps/app-1",
        headers=_auth("user-1"),
        json={"name": "New App", "description": "Updated", "category": "system"},
    )
    assert update.status_code == 200
    assert update.json()["app"]["name"] == "New App"
    assert update.json()["app"]["slug"] == "new-app"
    assert update.json()["app"]["description"] == "Updated"

    other_delete = client.delete("/studio/user-apps/app-1", headers=_auth("user-2"))
    assert other_delete.status_code == 404

    delete = client.delete("/studio/user-apps/app-1", headers=_auth("user-1"))
    assert delete.status_code == 200
    assert supabase.tables["user_apps"] == []
