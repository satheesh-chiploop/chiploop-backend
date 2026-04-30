from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .entitlements import PLAN_DEFINITIONS
from .models import CreditLedgerEntry, Plan, SubscriptionRecord


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()


class BillingRepository:
    def get_plan(self, plan_id: str) -> Optional[Plan]:
        raise NotImplementedError

    def get_user_plan_id(self, user_id: str) -> Optional[str]:
        raise NotImplementedError

    def get_user_subscription(self, user_id: str) -> Optional[SubscriptionRecord]:
        raise NotImplementedError

    def update_user_subscription(self, subscription_id: str, patch: Dict[str, Any]) -> Optional[SubscriptionRecord]:
        raise NotImplementedError

    def get_monthly_credit_usage(self, user_id: str) -> int:
        raise NotImplementedError

    def insert_credit_entry(self, entry: CreditLedgerEntry) -> None:
        raise NotImplementedError

    def count_active_api_keys(self, user_id: str) -> int:
        raise NotImplementedError

    def count_private_agents(self, user_id: str) -> int:
        raise NotImplementedError


class InMemoryBillingRepository(BillingRepository):
    def __init__(self, *, default_plan_id: str = "trial"):
        self.default_plan_id = default_plan_id
        self.user_plan_ids: Dict[str, str] = {}
        self.subscriptions: Dict[str, SubscriptionRecord] = {}
        self.credit_entries: List[CreditLedgerEntry] = []
        self.api_key_counts: Dict[str, int] = {}
        self.private_agent_counts: Dict[str, int] = {}

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        return PLAN_DEFINITIONS.get(plan_id)

    def get_user_plan_id(self, user_id: str) -> Optional[str]:
        subscription = self.get_user_subscription(user_id)
        if subscription:
            return subscription.plan_id
        return self.user_plan_ids.get(user_id, self.default_plan_id)

    def set_user_plan(self, user_id: str, plan_id: str) -> None:
        self.user_plan_ids[user_id] = plan_id

    def set_user_subscription(self, subscription: SubscriptionRecord) -> None:
        subscription.id = subscription.id or f"sub-{subscription.user_id}"
        self.subscriptions[subscription.user_id] = subscription

    def get_user_subscription(self, user_id: str) -> Optional[SubscriptionRecord]:
        return self.subscriptions.get(user_id)

    def update_user_subscription(self, subscription_id: str, patch: Dict[str, Any]) -> Optional[SubscriptionRecord]:
        for user_id, subscription in self.subscriptions.items():
            if subscription.id == subscription_id:
                updated = SubscriptionRecord.from_dict({**subscription.to_dict(), **patch})
                self.subscriptions[user_id] = updated
                return updated
        return None

    def get_monthly_credit_usage(self, user_id: str) -> int:
        return sum(-entry.credits_delta for entry in self.credit_entries if entry.user_id == user_id and entry.credits_delta < 0)

    def insert_credit_entry(self, entry: CreditLedgerEntry) -> None:
        self.credit_entries.append(entry)

    def count_active_api_keys(self, user_id: str) -> int:
        return self.api_key_counts.get(user_id, 0)

    def count_private_agents(self, user_id: str) -> int:
        return self.private_agent_counts.get(user_id, 0)


class SupabaseBillingRepository(BillingRepository):
    def __init__(self, supabase):
        self.supabase = supabase

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        try:
            result = (
                self.supabase.table("subscription_plans")
                .select("*")
                .eq("id", plan_id)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            return Plan.from_dict(rows[0]) if rows else PLAN_DEFINITIONS.get(plan_id)
        except Exception:
            return PLAN_DEFINITIONS.get(plan_id)

    def get_user_plan_id(self, user_id: str) -> Optional[str]:
        subscription = self.get_user_subscription(user_id)
        if subscription:
            return subscription.plan_id
        return "trial"

    def get_user_subscription(self, user_id: str) -> Optional[SubscriptionRecord]:
        try:
            result = (
                self.supabase.table("user_subscriptions")
                .select("*")
                .eq("user_id", user_id)
                .in_("status", ["active", "trialing"])
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            return SubscriptionRecord.from_dict(rows[0]) if rows else None
        except Exception:
            return None

    def update_user_subscription(self, subscription_id: str, patch: Dict[str, Any]) -> Optional[SubscriptionRecord]:
        try:
            result = (
                self.supabase.table("user_subscriptions")
                .update(patch)
                .eq("id", subscription_id)
                .execute()
            )
            rows = result.data or []
            return SubscriptionRecord.from_dict(rows[0]) if rows else None
        except Exception:
            return None

    def get_monthly_credit_usage(self, user_id: str) -> int:
        try:
            result = (
                self.supabase.table("credit_ledger")
                .select("credits_delta")
                .eq("user_id", user_id)
                .gte("created_at", _month_start())
                .lt("credits_delta", 0)
                .execute()
            )
            return sum(-int(row.get("credits_delta") or 0) for row in (result.data or []))
        except Exception:
            return 0

    def insert_credit_entry(self, entry: CreditLedgerEntry) -> None:
        self.supabase.table("credit_ledger").insert(entry.to_dict()).execute()

    def count_active_api_keys(self, user_id: str) -> int:
        try:
            result = (
                self.supabase.table("api_keys")
                .select("id")
                .eq("user_id", user_id)
                .is_("revoked_at", "null")
                .execute()
            )
            return len(result.data or [])
        except Exception:
            return 0

    def count_private_agents(self, user_id: str) -> int:
        try:
            result = (
                self.supabase.table("agents")
                .select("id")
                .eq("owner_id", user_id)
                .eq("is_custom", True)
                .eq("visibility", "private")
                .execute()
            )
            return len(result.data or [])
        except Exception:
            return 0
