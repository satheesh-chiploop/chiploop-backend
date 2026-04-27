import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth_api_keys.middleware import require_sdk_api_key
from auth_api_keys.models import APIKeyRecord
from auth_api_keys.service import APIKeyService, InMemoryAPIKeyStore, configure_api_key_service, hash_api_key, key_prefix
from chiploop_sdk.client import ChipLoopClient


def _app_with_auth(service: APIKeyService) -> FastAPI:
    configure_api_key_service(service)
    app = FastAPI()

    @app.get("/sdk/agents", dependencies=[Depends(require_sdk_api_key("sdk_agents_list"))])
    def sdk_agents():
        return {"ok": True}

    @app.get("/sdk/workflows/{workflow_id}/status", dependencies=[Depends(require_sdk_api_key("sdk_workflow_status"))])
    def sdk_status(workflow_id: str):
        return {"workflow_id": workflow_id}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def test_missing_api_key_rejected_on_sdk():
    client = TestClient(_app_with_auth(APIKeyService(InMemoryAPIKeyStore())))

    response = client.get("/sdk/agents")

    assert response.status_code == 401


def test_invalid_api_key_rejected():
    client = TestClient(_app_with_auth(APIKeyService(InMemoryAPIKeyStore())))

    response = client.get("/sdk/agents", headers={"Authorization": "Bearer cl_test_missing"})

    assert response.status_code == 401


def test_revoked_api_key_rejected():
    store = InMemoryAPIKeyStore()
    raw = "cl_test_revoked"
    store.save(
        APIKeyRecord(
            id="revoked",
            key_hash=hash_api_key(raw),
            key_prefix=key_prefix(raw),
            user_id="user-1",
            name="revoked",
            created_at="2026-01-01T00:00:00",
            revoked_at="2026-01-02T00:00:00",
        )
    )
    client = TestClient(_app_with_auth(APIKeyService(store)))

    response = client.get("/sdk/agents", headers={"Authorization": f"Bearer {raw}"})

    assert response.status_code == 401


def test_valid_api_key_accepted_and_usage_recorded():
    store = InMemoryAPIKeyStore()
    service = APIKeyService(store)
    raw, record = service.create_key("user-1", "test")
    client = TestClient(_app_with_auth(service))

    response = client.get("/sdk/workflows/wf-1/status", headers={"Authorization": f"Bearer {raw}"})

    assert response.status_code == 200
    assert store.records[record.key_hash].last_used_at
    assert len(store.usage_events) == 1
    assert store.usage_events[0].event_type == "sdk_workflow_status"
    assert store.usage_events[0].workflow_id == "wf-1"


def test_app_non_sdk_endpoint_not_affected():
    client = TestClient(_app_with_auth(APIKeyService(InMemoryAPIKeyStore())))

    response = client.get("/health")

    assert response.status_code == 200


def test_raw_api_key_is_not_stored():
    store = InMemoryAPIKeyStore()
    service = APIKeyService(store)
    raw, record = service.create_key("user-1", "test")

    stored = store.records[record.key_hash]

    assert raw.startswith("cl_test_")
    assert stored.key_hash != raw
    assert raw not in str(stored.to_dict())


def test_sdk_sends_bearer_token():
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"status": "ok", "agents": [], "count": 0}

    class FakeSession:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            return FakeResponse()

    session = FakeSession()
    client = ChipLoopClient("https://chiploop.example", api_key="cl_test_abc", session=session)
    client.list_agents()

    assert session.calls[0]["headers"]["Authorization"] == "Bearer cl_test_abc"
