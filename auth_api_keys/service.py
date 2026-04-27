import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import APIKeyRecord, APIKeyValidation, Entitlement, UsageEvent
from .repositories import APIKeyRepository, SupabaseAPIKeyRepository, SupabaseUsageRepository, UsageRepository


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

    def list_by_user(self, user_id: str) -> List[APIKeyRecord]:
        raise NotImplementedError

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        raise NotImplementedError

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        raise NotImplementedError

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        raise NotImplementedError

    def record_usage(self, event: UsageEvent) -> None:
        raise NotImplementedError

    def list_usage_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
        raise NotImplementedError


class InMemoryAPIKeyStore(APIKeyStore):
    def __init__(self):
        self.records: Dict[str, APIKeyRecord] = {}
        self.usage_events: List[UsageEvent] = []

    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        return self.records.get(key_hash)

    def list_by_user(self, user_id: str) -> List[APIKeyRecord]:
        records = [record for record in self.records.values() if record.user_id == user_id]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        self.records[record.key_hash] = record
        return record

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        for record in self.records.values():
            if record.id == record_id:
                record.last_used_at = timestamp
                return

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        for record in self.records.values():
            if record.id == record_id and record.user_id == user_id:
                record.revoked_at = timestamp
                return True
        return False

    def record_usage(self, event: UsageEvent) -> None:
        self.usage_events.append(event)

    def list_usage_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
        events = [event for event in self.usage_events if event.user_id == user_id]
        return sorted(events, key=lambda event: event.created_at, reverse=True)[:limit]


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

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        revoked = super().revoke(record_id, user_id, timestamp)
        if revoked:
            self._flush()
        return revoked

    def record_usage(self, event: UsageEvent) -> None:
        super().record_usage(event)
        self._flush()


class SupabaseAPIKeyStore(APIKeyStore):
    def __init__(self, supabase_client):
        self.api_keys = SupabaseAPIKeyRepository(supabase_client)
        self.usage = SupabaseUsageRepository(supabase_client)

    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        return self.api_keys.get_by_hash(key_hash)

    def list_by_user(self, user_id: str) -> List[APIKeyRecord]:
        return self.api_keys.list_by_user(user_id)

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        return self.api_keys.save(record)

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        self.api_keys.update_last_used(record_id, timestamp)

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        return self.api_keys.revoke(record_id, user_id, timestamp)

    def record_usage(self, event: UsageEvent) -> None:
        self.usage.record_usage(event)

    def list_usage_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
        return self.usage.list_by_user(user_id, limit=limit)


class RepositoryBackedAPIKeyStore(APIKeyStore):
    def __init__(self, api_keys: APIKeyRepository, usage: UsageRepository):
        self.api_keys = api_keys
        self.usage = usage

    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        return self.api_keys.get_by_hash(key_hash)

    def list_by_user(self, user_id: str) -> List[APIKeyRecord]:
        return self.api_keys.list_by_user(user_id)

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        return self.api_keys.save(record)

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        self.api_keys.update_last_used(record_id, timestamp)

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        return self.api_keys.revoke(record_id, user_id, timestamp)

    def record_usage(self, event: UsageEvent) -> None:
        self.usage.record_usage(event)

    def list_usage_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
        return self.usage.list_by_user(user_id, limit=limit)


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

    def list_key_metadata(self, user_id: str) -> List[dict]:
        return [_key_metadata(record) for record in self.store.list_by_user(user_id)]

    def revoke_key(self, record_id: str, user_id: str) -> bool:
        return self.store.revoke(record_id, user_id, _utcnow())

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

    def usage_summary(self, user_id: str, *, limit: int = 50) -> dict:
        events = self.store.list_usage_by_user(user_id, limit=limit)
        by_type: Dict[str, int] = {}
        for event in events:
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
        return {
            "user_id": user_id,
            "recent_event_count": len(events),
            "by_event_type": by_type,
            "recent_events": [event.to_dict() for event in events],
        }


def _key_metadata(record: APIKeyRecord) -> dict:
    return {
        "id": record.id,
        "key_prefix": record.key_prefix,
        "name": record.name,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "revoked_at": record.revoked_at,
    }


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
