import os
from functools import lru_cache
from typing import Any, Optional

from .auth import OIDCAuthAdapter, SupabaseAuthAdapter
from .database import PostgresDatabaseAdapter, SupabaseDatabaseAdapter
from .storage import AzureBlobStorageAdapter, LocalStorageAdapter, S3StorageAdapter, SupabaseStorageAdapter


class PlatformClient:
    def __init__(self, database: Any, storage: Any, auth: Any, raw_supabase: Any = None):
        self.database = database
        self.storage = storage
        self.auth = auth
        self.raw_supabase = raw_supabase

    def table(self, name: str) -> Any:
        return self.database.table(name)

    def rpc(self, name: str, params: Optional[dict] = None) -> Any:
        if self.raw_supabase is not None and hasattr(self.raw_supabase, "rpc"):
            return self.raw_supabase.rpc(name, params or {})
        raise NotImplementedError(f"RPC {name!r} is only available with the Supabase/PostgREST database provider")


def _supabase_client() -> Any:
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("supabase-py is required for Supabase platform adapter") from exc
    url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("Supabase platform adapter requires SUPABASE_URL and a Supabase key")
    return create_client(url, key)


def create_platform_client() -> PlatformClient:
    database_provider = os.getenv("CHIPLOOP_DATABASE_PROVIDER", "supabase").strip().lower()
    storage_provider = os.getenv("CHIPLOOP_STORAGE_PROVIDER", "supabase").strip().lower()
    auth_provider = os.getenv("CHIPLOOP_AUTH_PROVIDER", "supabase").strip().lower()

    needs_supabase = "supabase" in {database_provider, storage_provider, auth_provider}
    raw_supabase = _supabase_client() if needs_supabase else None

    if database_provider in {"supabase", "postgrest"}:
        database = SupabaseDatabaseAdapter(raw_supabase)
    elif database_provider in {"postgres", "postgresql"}:
        database = PostgresDatabaseAdapter()
    else:
        raise RuntimeError(f"Unsupported CHIPLOOP_DATABASE_PROVIDER: {database_provider}")

    if storage_provider == "supabase":
        storage = SupabaseStorageAdapter(raw_supabase.storage)
    elif storage_provider in {"local", "local_fs", "filesystem"}:
        storage = LocalStorageAdapter()
    elif storage_provider in {"s3", "minio"}:
        storage = S3StorageAdapter()
    elif storage_provider in {"azure_blob", "azure"}:
        storage = AzureBlobStorageAdapter()
    else:
        raise RuntimeError(f"Unsupported CHIPLOOP_STORAGE_PROVIDER: {storage_provider}")

    if auth_provider == "supabase":
        auth = SupabaseAuthAdapter(raw_supabase.auth)
    elif auth_provider in {"oidc", "okta", "auth0", "azure_ad", "entra_id"}:
        auth = OIDCAuthAdapter()
    elif auth_provider == "disabled":
        auth = None
    else:
        raise RuntimeError(f"Unsupported CHIPLOOP_AUTH_PROVIDER: {auth_provider}")

    return PlatformClient(database, storage, auth, raw_supabase=raw_supabase)


@lru_cache(maxsize=1)
def get_platform_client() -> PlatformClient:
    return create_platform_client()
