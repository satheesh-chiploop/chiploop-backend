import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import APIKeyRecord, APIKeyValidation, Entitlement, UsageEvent


API_KEY_PREFIXES = ("cl_live_", "cl_test_")
LOCAL_KEY_STORE_ENV = "CHIPLOOP_LOCAL_API_KEY_STORE"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_raw_api_key(test: bool = True) -> str:
    prefix = "cl_test_" if test else "cl_live_"
    return prefix + secrets.token_urlsafe(32)


def key_prefix(raw_key: str) -> str:
    return raw_key[:16]


class APIKeyStore:
    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        raise NotImplementedError

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        raise NotImplementedError

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        raise NotImplementedError

    def record_usage(self, event: UsageEvent) -> None:
        raise NotImplementedError


class InMemoryAPIKeyStore(APIKeyStore):
    def __init__(self):
        self.records: Dict[str, APIKeyRecord] = {}
        self.usage_events: List[UsageEvent] = []

    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        return self.records.get(key_hash)

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        self.records[record.key_hash] = record
        return record

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        for record in self.records.values():
            if record.id == record_id:
                record.last_used_at = timestamp
                return

    def record_usage(self, event: UsageEvent) -> None:
        self.usage_events.append(event)


class JsonFileAPIKeyStore(InMemoryAPIKeyStore):
    def __init__(self, path: str):
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.records = {
            row["key_hash"]: APIKeyRecord.from_dict(row)
            for row in data.get("api_keys", [])
        }
        self.usage_events = [UsageEvent(**row) for row in data.get("usage_events", [])]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "api_keys": [record.to_dict() for record in self.records.values()],
                    "usage_events": [event.to_dict() for event in self.usage_events],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        saved = super().save(record)
        self._flush()
        return saved

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        super().update_last_used(record_id, timestamp)
        self._flush()

    def record_usage(self, event: UsageEvent) -> None:
        super().record_usage(event)
        self._flush()


class SupabaseAPIKeyStore(APIKeyStore):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        result = (
            self.supabase.table("api_keys")
            .select("id,key_hash,key_prefix,user_id,name,created_at,revoked_at,last_used_at")
            .eq("key_hash", key_hash)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return APIKeyRecord.from_dict(rows[0]) if rows else None

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        self.supabase.table("api_keys").insert(record.to_dict()).execute()
        return record

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        self.supabase.table("api_keys").update({"last_used_at": timestamp}).eq("id", record_id).execute()

    def record_usage(self, event: UsageEvent) -> None:
        self.supabase.table("api_usage_events").insert(event.to_dict()).execute()


class APIKeyService:
    def __init__(self, store: APIKeyStore):
        self.store = store

    def create_key(self, user_id: str, name: str, *, test: bool = True) -> tuple[str, APIKeyRecord]:
        raw_key = generate_raw_api_key(test=test)
        record = APIKeyRecord(
            id=hash_api_key(raw_key)[:24],
            key_hash=hash_api_key(raw_key),
            key_prefix=key_prefix(raw_key),
            user_id=user_id,
            name=name,
            created_at=_utcnow(),
        )
        self.store.save(record)
        return raw_key, record

    def validate_key(self, raw_key: str) -> APIKeyValidation:
        if not raw_key or not raw_key.startswith(API_KEY_PREFIXES):
            return APIKeyValidation(False, error="invalid_api_key_format")
        try:
            record = self.store.get_by_hash(hash_api_key(raw_key))
        except Exception:
            return APIKeyValidation(False, error="api_key_validation_failed")
        if not record:
            return APIKeyValidation(False, error="invalid_api_key")
        if record.revoked_at:
            return APIKeyValidation(False, record=record, error="api_key_revoked")
        try:
            self.store.update_last_used(record.id, _utcnow())
        except Exception:
            pass
        return APIKeyValidation(True, record=record)

    def record_usage(
        self,
        *,
        user_id: str,
        api_key_id: str,
        endpoint: str,
        event_type: str,
        workflow_id: Optional[str] = None,
    ) -> None:
        try:
            self.store.record_usage(
                UsageEvent(
                    user_id=user_id,
                    api_key_id=api_key_id,
                    endpoint=endpoint,
                    event_type=event_type,
                    workflow_id=workflow_id,
                    created_at=_utcnow(),
                )
            )
        except Exception:
            pass

    def get_entitlement(self, user_id: str) -> Entitlement:
        return Entitlement(
            sdk_cli_enabled=True,
            monthly_credit_limit=1_000_000,
            agent_factory_write_enabled=os.getenv("CHIPLOOP_AGENT_FACTORY_WRITE_ENABLED", "").lower()
            in {"1", "true", "yes"},
        )


_service: Optional[APIKeyService] = None


def build_api_key_service(supabase_client=None) -> APIKeyService:
    local_store_path = os.getenv(LOCAL_KEY_STORE_ENV)
    if local_store_path:
        return APIKeyService(JsonFileAPIKeyStore(local_store_path))
    if supabase_client is not None:
        return APIKeyService(SupabaseAPIKeyStore(supabase_client))
    return APIKeyService(InMemoryAPIKeyStore())


def configure_api_key_service(service: APIKeyService) -> None:
    global _service
    _service = service


def get_api_key_service(supabase_client=None) -> APIKeyService:
    global _service
    if _service is None:
        _service = build_api_key_service(supabase_client=supabase_client)
    return _service


def create_api_key(user_id: str, name: str, *, test: bool = True) -> tuple[str, APIKeyRecord]:
    return get_api_key_service().create_key(user_id, name, test=test)
