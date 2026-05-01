from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .models import SubscriptionRecord


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def trial_days_remaining(subscription: Optional[SubscriptionRecord]) -> Optional[int]:
    if not subscription or subscription.trial_status != "active":
        return None
    end = _parse_dt(subscription.trial_end_at)
    if not end:
        return None
    delta = end - datetime.now(timezone.utc)
    return max(0, delta.days + (1 if delta.seconds > 0 else 0))


def build_upgrade_decision(
    *,
    plan_id: str,
    credits_remaining: Optional[int],
    monthly_credits: Optional[int],
    subscription: Optional[SubscriptionRecord],
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    days = trial_days_remaining(subscription)

    if subscription and subscription.trial_status == "expired":
        return {
            "show": True,
            "type": "trial_expired",
            "target_plan": "Starter",
            "message": "Your trial has ended. Starter is $19.99/month after the 7-day trial, with 25% off for the first 3 months.",
            "reason": reason or "trial_expired",
        }

    if days is not None and days <= 5:
        message = (
            f"Your trial ends in {days} day{'s' if days != 1 else ''}. "
            "Starter is $19.99/month after 7 days, with 25% off for the first 3 months. Cancel anytime before the trial ends."
        )
        return {
            "show": True,
            "type": "trial_expiring",
            "target_plan": "Starter",
            "message": message,
            "reason": reason or "trial_expiring",
        }

    if monthly_credits and credits_remaining is not None and credits_remaining <= max(50, monthly_credits * 0.1):
        target = "Pro Max" if plan_id == "pro" else "Pro"
        return {
            "show": True,
            "type": "low_credits",
            "target_plan": target,
            "message": f"You have {credits_remaining} credits remaining this month. {target} gives more monthly credits.",
            "reason": reason or "low_credits",
        }

    if reason == "feature_limit":
        target = "Pro" if plan_id in {"trial", "free", "starter"} else "Pro Max"
        return {
            "show": True,
            "type": "feature_limit",
            "target_plan": target,
            "message": f"This feature is not enabled on your current plan. Upgrade to {target} to continue.",
            "reason": reason,
        }

    return {
        "show": False,
        "type": None,
        "target_plan": None,
        "message": "",
        "reason": reason,
    }
