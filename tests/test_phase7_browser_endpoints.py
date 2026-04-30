from datetime import datetime, timezone

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_auth
import browser_routes
from auth_api_keys.models import UsageEvent
from auth_api_keys.service import APIKeyService, InMemoryAPIKeyStore
from billing import BillingService, InMemoryBillingRepository


JWT_SECRET = "phase7-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _client(service: APIKeyService | None = None) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.api_key_service = service or APIKeyService(InMemoryAPIKeyStore())
    app.state.billing_service = BillingService(InMemoryBillingRepository(default_plan_id="pro"))
    app.include_router(browser_routes.router)
    return TestClient(app)


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def test_unauthenticated_studio_request_rejected():
    client = _client()
    response = client.get("/studio/registry/summary")
    assert response.status_code == 401


def test_unauthenticated_settings_request_rejected():
    client = _client()
    response = client.get("/settings/api-keys")
    assert response.status_code == 401


def test_studio_registry_summary_returns_safe_counts():
    client = _client()
    response = client.get("/studio/registry/summary", headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert data["agent_count"] >= 151
    assert data["workflow_count"] >= 1
    assert "agents" not in data
    assert "raw_yaml" not in data


def test_studio_agent_planner_returns_frontend_safe_result():
    client = _client()
    response = client.post(
        "/studio/agent-planner/plan",
        headers=_auth(),
        json={"natural_language_request": "Generate Digital RTL Agent", "domain": "digital"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert "exact_matches" in result
    assert "similar_matches" in result
    assert "reusable_skills" in result
    assert "reusable_tools" in result
    assert "reusable_hooks" in result
    assert "recommendation" in result
    assert "confidence_score" in result
    assert "explanation" in result


def test_studio_agent_factory_dry_run_forces_no_writes(monkeypatch):
    called = {}

    class FakeResult:
        def to_dict(self):
            return {
                "ok": True,
                "dry_run": False,
                "plan": {"validation_checklist": ["checked"], "risk_notes": []},
                "written_files": ["generated_studio_agents/unsafe.py"],
                "errors": [],
            }

    def fake_factory(request, *, dry_run=True, **kwargs):
        called["dry_run"] = dry_run
        return FakeResult()

    monkeypatch.setattr(browser_routes, "run_studio_factory", fake_factory)
    client = _client()
    response = client.post(
        "/studio/agent-factory/dry-run",
        headers=_auth(),
        json={"name": "New Agent", "natural_language_request": "make it", "dry_run": False},
    )
    assert response.status_code == 200
    assert called["dry_run"] is True
    result = response.json()["result"]
    assert result["dry_run"] is True
    assert result["written_files"] == []


def test_studio_dag_preview_does_not_execute_agents(monkeypatch):
    def fail_if_executor_used(*args, **kwargs):
        raise AssertionError("DAG preview must not execute agents")

    monkeypatch.setattr("workflow_dag.executor.execute_dag", fail_if_executor_used)
    client = _client()
    response = client.post(
        "/studio/dag/preview",
        headers=_auth(),
        json={"agents": ["Digital Spec Agent", "Digital RTL Agent"], "loop_type": "digital"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["preview"]["dry_run"] is True
    assert "parallel_groups" in body["preview"]


def test_api_key_list_returns_metadata_only():
    service = APIKeyService(InMemoryAPIKeyStore())
    raw, _ = service.create_key("user-1", "local")
    client = _client(service)
    response = client.get("/settings/api-keys", headers=_auth("user-1"))
    assert response.status_code == 200
    item = response.json()["api_keys"][0]
    assert item["name"] == "local"
    assert "key_hash" not in item
    assert raw not in str(response.json())


def test_api_key_create_returns_raw_once_and_stores_only_hash():
    service = APIKeyService(InMemoryAPIKeyStore())
    client = _client(service)
    response = client.post("/settings/api-keys", headers=_auth("user-1"), json={"name": "browser"})
    assert response.status_code == 200
    raw = response.json()["api_key"]
    assert raw.startswith("cl_test_")
    stored = list(service.store.records.values())[0]
    assert stored.key_hash
    assert stored.key_hash != raw
    assert raw not in str(stored.to_dict())


def test_api_key_revoke_enforces_ownership():
    service = APIKeyService(InMemoryAPIKeyStore())
    _, owned = service.create_key("user-1", "owned")
    _, other = service.create_key("user-2", "other")
    client = _client(service)

    forbidden = client.post(f"/settings/api-keys/{other.id}/revoke", headers=_auth("user-1"))
    assert forbidden.status_code == 404

    response = client.post(f"/settings/api-keys/{owned.id}/revoke", headers=_auth("user-1"))
    assert response.status_code == 200
    assert service.store.list_by_user("user-1")[0].revoked_at is not None


def test_usage_endpoint_returns_user_scoped_data():
    store = InMemoryAPIKeyStore()
    store.record_usage(
        UsageEvent(
            user_id="user-1",
            api_key_id="key-1",
            endpoint="/sdk/agents",
            event_type="sdk_agents_list",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    store.record_usage(
        UsageEvent(
            user_id="user-2",
            api_key_id="key-2",
            endpoint="/sdk/workflows",
            event_type="sdk_workflows_list",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    client = _client(APIKeyService(store))
    response = client.get("/settings/usage", headers=_auth("user-1"))
    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["recent_event_count"] == 1
    assert usage["recent_events"][0]["user_id"] == "user-1"
