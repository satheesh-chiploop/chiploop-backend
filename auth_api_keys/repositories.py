from typing import Optional

from .models import APIKeyRecord, UsageEvent


class APIKeyRepository:
    def get_by_hash(self, key_hash: str) -> Optional[APIKeyRecord]:
        raise NotImplementedError

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        raise NotImplementedError

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        raise NotImplementedError


class UsageRepository:
    def record_usage(self, event: UsageEvent) -> None:
        raise NotImplementedError


class SupabaseAPIKeyRepository(APIKeyRepository):
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


class SupabaseUsageRepository(UsageRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def record_usage(self, event: UsageEvent) -> None:
        self.supabase.table("api_usage_events").insert(event.to_dict()).execute()


class PlanRepository:
    """
    Placeholder repository boundary for future persisted plans/subscriptions.

    Phase 6 adds SQL only; backend plan enforcement remains the existing minimal
    entitlement stub until billing work is explicitly enabled.
    """

    def get_entitlement_for_user(self, user_id: str):
        raise NotImplementedError
