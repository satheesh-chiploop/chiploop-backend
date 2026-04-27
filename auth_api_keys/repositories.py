from typing import List, Optional

from .models import APIKeyRecord, UsageEvent


class APIKeyRepository:
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


class UsageRepository:
    def record_usage(self, event: UsageEvent) -> None:
        raise NotImplementedError

    def list_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
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

    def list_by_user(self, user_id: str) -> List[APIKeyRecord]:
        result = (
            self.supabase.table("api_keys")
            .select("id,key_hash,key_prefix,user_id,name,created_at,revoked_at,last_used_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [APIKeyRecord.from_dict(row) for row in (result.data or [])]

    def save(self, record: APIKeyRecord) -> APIKeyRecord:
        self.supabase.table("api_keys").insert(record.to_dict()).execute()
        return record

    def update_last_used(self, record_id: str, timestamp: str) -> None:
        self.supabase.table("api_keys").update({"last_used_at": timestamp}).eq("id", record_id).execute()

    def revoke(self, record_id: str, user_id: str, timestamp: str) -> bool:
        result = (
            self.supabase.table("api_keys")
            .update({"revoked_at": timestamp})
            .eq("id", record_id)
            .eq("user_id", user_id)
            .execute()
        )
        return bool(result.data)


class SupabaseUsageRepository(UsageRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def record_usage(self, event: UsageEvent) -> None:
        self.supabase.table("api_usage_events").insert(event.to_dict()).execute()

    def list_by_user(self, user_id: str, limit: int = 50) -> List[UsageEvent]:
        result = (
            self.supabase.table("api_usage_events")
            .select("user_id,api_key_id,endpoint,event_type,workflow_id,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [UsageEvent(**row) for row in (result.data or [])]


class PlanRepository:
    """
    Placeholder repository boundary for future persisted plans/subscriptions.

    Phase 6 adds SQL only; backend plan enforcement remains the existing minimal
    entitlement stub until billing work is explicitly enabled.
    """

    def get_entitlement_for_user(self, user_id: str):
        raise NotImplementedError
