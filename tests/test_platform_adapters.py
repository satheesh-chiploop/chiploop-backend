from pathlib import Path

from artifact_policy import artifact_may_sync
from browser_auth import BrowserUser
from platform_adapters.client import create_platform_client
from platform_adapters.database import PostgresQuery
from platform_adapters.storage import LocalStorageAdapter
from platform_browser_api import PlatformQueryRequest, platform_query


class FakePostgresAdapter:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None, fetch=False):
        self.calls.append((sql, list(params or []), fetch))
        return [{"id": "wf-1", "status": "saved"}]


class RecordingQuery:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return method

    def execute(self):
        return type("Response", (), {"data": [], "count": None})()


class RecordingPlatformClient:
    def __init__(self):
        self.queries = []

    def table(self, name):
        query = RecordingQuery(name)
        self.queries.append(query)
        return query


def test_local_storage_preserves_supabase_bucket_shape(tmp_path):
    storage = LocalStorageAdapter(str(tmp_path))
    bucket = storage.from_("artifacts")
    bucket.upload("backend/workflows/wf-1/report.md", b"hello")
    assert bucket.download("backend/workflows/wf-1/report.md") == b"hello"
    names = {entry["name"] for entry in bucket.list("backend/workflows/wf-1")}
    assert "report.md" in names


def test_postgres_query_supports_common_supabase_chain():
    adapter = FakePostgresAdapter()
    response = (
        PostgresQuery(adapter, "workflows")
        .select("id,status", count="exact")
        .eq("user_id", "user-1")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    sql, params, fetch = adapter.calls[0]
    assert 'SELECT "id", "status" FROM "workflows"' in sql
    assert '"user_id" = %s' in sql
    assert 'ORDER BY "created_at" DESC LIMIT 1' in sql
    assert params == ["user-1"]
    assert fetch is True
    assert response.count == 1


def test_postgres_query_keeps_multiple_or_calls_as_and_groups():
    adapter = FakePostgresAdapter()
    (
        PostgresQuery(adapter, "workflows")
        .select("id")
        .or_("user_id.eq.user-1,is_prebuilt.eq.true")
        .or_("is_prebuilt.eq.false,is_prebuilt.is.null")
        .execute()
    )
    sql, params, _ = adapter.calls[0]
    assert '("user_id" = %s OR "is_prebuilt" = %s)' in sql
    assert '("is_prebuilt" = %s OR "is_prebuilt" IS NULL)' in sql
    assert params == ["user-1", True, False]


def test_create_platform_client_supports_postgres_local_disabled_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIPLOOP_DATABASE_PROVIDER", "postgresql")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/chiploop")
    monkeypatch.setenv("CHIPLOOP_STORAGE_PROVIDER", "local_fs")
    monkeypatch.setenv("CHIPLOOP_PRIVATE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CHIPLOOP_AUTH_PROVIDER", "disabled")
    client = create_platform_client()
    assert client.auth is None
    assert client.storage.from_("artifacts").root == Path(tmp_path) / "artifacts"
    assert client.table("workflows").table_name == "workflows"


def test_customer_cloud_storage_syncs_to_selected_customer_storage():
    assert artifact_may_sync("rtl.sv", "customer_cloud_storage") is True
    assert artifact_may_sync("rtl.sv", "full_private") is False


def test_browser_platform_workflow_query_adds_owner_or_prebuilt_scope(monkeypatch):
    client = RecordingPlatformClient()
    monkeypatch.setattr("platform_browser_api.get_platform_client", lambda: client)
    result = platform_query(
        PlatformQueryRequest(table="workflows", columns="id,status"),
        BrowserUser(user_id="user-1", claims={"sub": "user-1"}),
    )
    assert result["data"] == []
    calls = client.queries[0].calls
    assert ("select", ("id,status",), {"count": None}) in calls
    assert ("or_", ("user_id.eq.user-1,is_prebuilt.eq.true",), {}) in calls
