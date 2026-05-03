from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .entitlements import ACTION_CREDIT_COSTS, PLAN_DEFINITIONS, SDK_EVENT_TO_FEATURE, plan_payload
from .models import CreditLedgerEntry, Entitlements, Plan, UsageEstimate
from .repositories import BillingRepository, InMemoryBillingRepository, SupabaseBillingRepository
from .upgrade_logic import build_upgrade_decision, trial_days_remaining


class EntitlementDenied(Exception):
    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(feature)


class CreditLimitExceeded(Exception):
    def __init__(self, required: int, remaining: int):
        self.required = required
        self.remaining = remaining
        super().__init__(f"insufficient credits: required={required}, remaining={remaining}")


class BillingPaymentRequired(Exception):
    def __init__(self, billing_status: str, grace_period_end_at: Optional[str] = None):
        self.billing_status = billing_status
        self.grace_period_end_at = grace_period_end_at
        super().__init__(billing_status)


class TrialCheckoutRequired(Exception):
    def __init__(self):
        super().__init__("trial_checkout_required")


class BillingService:
    def __init__(self, repository: BillingRepository):
        self.repository = repository

    def _subscription(self, user_id: str):
        return self.repository.get_user_subscription(user_id)

    def get_user_plan(self, user_id: str) -> Plan:
        self.auto_convert_expired_trial(user_id)
        plan_id = self.repository.get_user_plan_id(user_id) or "account"
        return self.repository.get_plan(plan_id) or PLAN_DEFINITIONS["account"]

    def get_entitlements(self, user_id: str) -> Entitlements:
        return self.get_user_plan(user_id).entitlements

    def is_billing_blocked(self, user_id: str) -> bool:
        subscription = self._subscription(user_id)
        if not subscription or subscription.billing_status not in {"past_due", "unpaid", "payment_failed"}:
            return False
        if not subscription.grace_period_end_at:
            return True
        try:
            grace_end = datetime.fromisoformat(subscription.grace_period_end_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= grace_end

    def assert_billing_current(self, user_id: str) -> None:
        subscription = self._subscription(user_id)
        if subscription and self.is_billing_blocked(user_id):
            raise BillingPaymentRequired(subscription.billing_status, subscription.grace_period_end_at)

    def has_checkout_subscription(self, user_id: str) -> bool:
        if self._subscription(user_id) is not None:
            return True
        return (self.repository.get_user_plan_id(user_id) or "account") != "account"

    def assert_checkout_started(self, user_id: str) -> None:
        if not self.has_checkout_subscription(user_id):
            raise TrialCheckoutRequired()
        self.assert_billing_current(user_id)

    def discounted_price(self, plan: Plan) -> Optional[float]:
        if plan.price_monthly_usd is None:
            return None
        if not plan.discount_percent:
            return plan.price_monthly_usd
        return round(plan.price_monthly_usd * (1 - plan.discount_percent / 100), 2)

    def discount_months_remaining(self, user_id: str, plan: Optional[Plan] = None) -> int:
        plan = plan or self.get_user_plan(user_id)
        if not plan.discount_months:
            return 0
        subscription = self._subscription(user_id)
        cycle_count = int(subscription.billing_cycle_count if subscription else 0)
        return max(plan.discount_months - cycle_count, 0)

    def effective_price(self, user_id: str, plan: Optional[Plan] = None) -> Optional[float]:
        plan = plan or self.get_user_plan(user_id)
        if plan.price_monthly_usd is None:
            return None
        return self.discounted_price(plan) if self.discount_months_remaining(user_id, plan) > 0 else plan.price_monthly_usd

    def is_trial_active(self, user_id: str) -> bool:
        subscription = self._subscription(user_id)
        if not subscription:
            return self.repository.get_user_plan_id(user_id) in {"trial", "free", None}
        return subscription.trial_status == "active" and not self.is_trial_expired(user_id)

    def get_trial_days_remaining(self, user_id: str) -> Optional[int]:
        return trial_days_remaining(self._subscription(user_id))

    def is_trial_expired(self, user_id: str) -> bool:
        subscription = self._subscription(user_id)
        if not subscription or subscription.trial_status != "active" or not subscription.trial_end_at:
            return False
        try:
            end = datetime.fromisoformat(subscription.trial_end_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= end

    def auto_convert_expired_trial(self, user_id: str):
        subscription = self._subscription(user_id)
        if not subscription or subscription.trial_status != "active" or not self.is_trial_expired(user_id):
            return subscription
        if not subscription.auto_renew:
            return self.repository.update_user_subscription(
                subscription.id,
                {"trial_status": "expired", "status": "canceled", "billing_status": "trial_expired"},
            )
        updated = self.repository.update_user_subscription(
            subscription.id,
            {
                "plan_id": "starter",
                "status": "active",
                "trial_status": "converted",
                "billing_status": "trial_converted",
                "current_period_start": datetime.now(timezone.utc).isoformat(),
                "discount_end_at": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(),
                "plan_price_effective": self.discounted_price(PLAN_DEFINITIONS["starter"]),
                "billing_cycle_count": 0,
            },
        )
        self.repository.insert_credit_entry(
            CreditLedgerEntry(
                user_id=user_id,
                event_type="trial_auto_converted",
                credits_delta=0,
                reference_id=subscription.id,
                metadata={"from_plan": "trial", "to_plan": "starter"},
            )
        )
        return updated

    def get_credit_balance(self, user_id: str) -> Dict[str, Optional[int]]:
        plan = self.get_user_plan(user_id)
        used = self.repository.get_monthly_credit_usage(user_id)
        monthly = plan.monthly_credits
        remaining = None if monthly is None else max(monthly - used, 0)
        return {"monthly_credits": monthly, "credits_used": used, "credits_remaining": remaining}

    def estimate_credits(self, action_type: str, metadata: Optional[Dict[str, Any]] = None) -> UsageEstimate:
        return UsageEstimate(action_type=action_type, credits=ACTION_CREDIT_COSTS.get(action_type, 1), metadata=metadata or {})

    def assert_entitlement(self, user_id: str, feature: str) -> Entitlements:
        entitlements = self.get_entitlements(user_id)
        if not bool(getattr(entitlements, feature, False)):
            raise EntitlementDenied(feature)
        return entitlements

    def assert_sdk_event_allowed(self, user_id: str, event_type: str) -> Entitlements:
        self.assert_billing_current(user_id)
        feature = SDK_EVENT_TO_FEATURE.get(event_type, "sdk_cli_enabled")
        return self.assert_entitlement(user_id, feature)

    def assert_api_key_limit(self, user_id: str) -> Entitlements:
        self.assert_checkout_started(user_id)
        entitlements = self.get_entitlements(user_id)
        if self.repository.count_active_api_keys(user_id) >= entitlements.max_api_keys:
            raise EntitlementDenied("max_api_keys")
        return entitlements

    def assert_private_agent_limit(self, user_id: str) -> Entitlements:
        self.assert_checkout_started(user_id)
        entitlements = self.get_entitlements(user_id)
        if self.repository.count_private_agents(user_id) >= entitlements.max_private_agents:
            raise EntitlementDenied("max_private_agents")
        return entitlements

    def deduct_credits(
        self,
        user_id: str,
        action_type: str,
        amount: Optional[int] = None,
        reference_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        api_key_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> Dict[str, Optional[int]]:
        self.assert_billing_current(user_id)
        estimate = self.estimate_credits(action_type, metadata)
        credits = int(amount if amount is not None else estimate.credits)
        balance = self.get_credit_balance(user_id)
        remaining = balance["credits_remaining"]
        if remaining is not None and credits > remaining:
            raise CreditLimitExceeded(credits, remaining)

        balance_after = None if remaining is None else remaining - credits
        self.repository.insert_credit_entry(
            CreditLedgerEntry(
                user_id=user_id,
                event_type=action_type,
                credits_delta=-credits,
                reference_id=reference_id,
                api_key_id=api_key_id,
                workflow_id=workflow_id,
                balance_after=balance_after,
                metadata=metadata or {},
            )
        )
        return {
            **balance,
            "deducted": credits,
            "credits_remaining": balance_after,
        }

    def upgrade_status(self, user_id: str, *, reason: Optional[str] = None) -> Dict[str, Any]:
        self.auto_convert_expired_trial(user_id)
        plan = self.get_user_plan(user_id)
        balance = self.get_credit_balance(user_id)
        subscription = self._subscription(user_id)
        suggestion = build_upgrade_decision(
            plan_id=plan.id,
            credits_remaining=balance["credits_remaining"],
            monthly_credits=balance["monthly_credits"],
            subscription=subscription,
            reason=reason,
        )
        return {
            "current_plan": plan_payload(plan),
            "billing_blocked": self.is_billing_blocked(user_id),
            "trial_status": subscription.trial_status if subscription else ("active" if plan.id in {"trial", "free"} else None),
            "days_remaining": self.get_trial_days_remaining(user_id),
            "suggested_upgrade": suggestion,
            "upgrade": suggestion,
            "reason": suggestion.get("reason"),
        }

    def plan_summary(self, user_id: str) -> Dict[str, Any]:
        self.auto_convert_expired_trial(user_id)
        plan = self.get_user_plan(user_id)
        balance = self.get_credit_balance(user_id)
        subscription = self._subscription(user_id)
        effective_price = self.effective_price(user_id, plan)
        discounted_price = self.discounted_price(plan) if plan.discount_percent else None
        discount_months_remaining = self.discount_months_remaining(user_id, plan)
        trial = {
            "status": subscription.trial_status if subscription else ("active" if plan.id in {"trial", "free"} else None),
            "days_remaining": self.get_trial_days_remaining(user_id),
            "trial_start_at": subscription.trial_start_at if subscription else None,
            "trial_end_at": subscription.trial_end_at if subscription else None,
            "auto_renew": subscription.auto_renew if subscription else True,
            "converts_to": "Starter" if plan.id in {"trial", "free"} else None,
        }
        upgrade = self.upgrade_status(user_id)
        return {
            "current_plan": plan_payload(plan),
            "plan_name": plan.display_name,
            "base_price": "custom" if plan.price_monthly_usd is None else plan.price_monthly_usd,
            "discounted_price": discounted_price if discount_months_remaining > 0 else None,
            "price_monthly": effective_price,
            "price": "custom" if plan.price_monthly_usd is None else plan.price_monthly_usd,
            "credits": balance["monthly_credits"],
            "monthly_credits": balance["monthly_credits"],
            "credits_used": balance["credits_used"],
            "credits_remaining": balance["credits_remaining"],
            "trial_days_remaining": trial["days_remaining"],
            "discount_months_remaining": discount_months_remaining,
            "entitlements": plan.entitlements.to_dict(),
            "billing_status": subscription.billing_status if subscription else ("checkout_required" if plan.id == "account" else ("trial" if plan.id in {"trial", "free"} else "placeholder")),
            "billing_blocked": self.is_billing_blocked(user_id),
            "grace_period_end_at": subscription.grace_period_end_at if subscription else None,
            "requires_checkout": subscription is None,
            "can_run_workflows": subscription is not None and not self.is_billing_blocked(user_id),
            "trial": trial,
            "suggested_upgrade": upgrade["suggested_upgrade"],
            "upgrade_hint": upgrade["suggested_upgrade"],
        }


_service: Optional[BillingService] = None


def build_billing_service(supabase_client=None) -> BillingService:
    if supabase_client is not None:
        return BillingService(SupabaseBillingRepository(supabase_client))
    return BillingService(InMemoryBillingRepository())


def configure_billing_service(service: BillingService) -> None:
    global _service
    _service = service


def get_billing_service(supabase_client=None) -> BillingService:
    global _service
    if _service is None:
        _service = build_billing_service(supabase_client=supabase_client)
    return _service
