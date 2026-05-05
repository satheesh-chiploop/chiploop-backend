import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from billing.entitlements import PLAN_DEFINITIONS
from billing.repositories import BillingRepository


class StripeBillingConfigError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


class StripeBillingService:
    """Hosted Stripe Checkout/Portal integration.

    ChipLoop never receives card numbers. Stripe stores payment methods and sends
    subscription state back through signed webhooks.
    """

    def __init__(self, repository: BillingRepository, stripe_module=None):
        self.repository = repository
        self.stripe = stripe_module or self._load_stripe()
        self.secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        self.app_url = os.environ.get("CHIPLOOP_APP_URL", os.environ.get("FRONTEND_URL", "http://localhost:3000")).rstrip("/")
        self.trial_days = int(os.environ.get("STRIPE_TRIAL_DAYS", "7"))
        self.grace_days = int(os.environ.get("STRIPE_PAYMENT_GRACE_DAYS", "3"))
        self.coupon_id = os.environ.get("STRIPE_INTRO_COUPON_ID", "").strip() or None
        self.price_ids = {
            "starter": os.environ.get("STRIPE_PRICE_STARTER", "").strip(),
            "pro": os.environ.get("STRIPE_PRICE_PRO", "").strip(),
            "pro_max": os.environ.get("STRIPE_PRICE_PRO_MAX", "").strip(),
        }
        if self.secret_key:
            self.stripe.api_key = self.secret_key

    def _load_stripe(self):
        try:
            import stripe  # type: ignore
        except ImportError as exc:
            raise StripeBillingConfigError("stripe_package_missing") from exc
        return stripe

    def _require_configured(self) -> None:
        if not self.secret_key:
            raise StripeBillingConfigError("STRIPE_SECRET_KEY_missing")

    def _price_id(self, plan_id: str) -> str:
        price_id = self.price_ids.get(plan_id, "")
        if plan_id not in self.price_ids:
            raise ValueError("unsupported_checkout_plan")
        if not price_id:
            raise StripeBillingConfigError(f"STRIPE_PRICE_{plan_id.upper()}_missing")
        if price_id.startswith("prod_"):
            raise StripeBillingConfigError(
                f"STRIPE_PRICE_{plan_id.upper()}_uses_product_id_expected_price_id"
            )
        if not price_id.startswith("price_"):
            raise StripeBillingConfigError(
                f"STRIPE_PRICE_{plan_id.upper()}_invalid_expected_price_id"
            )
        return price_id

    def create_checkout_session(self, *, user_id: str, user_email: Optional[str], plan_id: str) -> Dict[str, Any]:
        self._require_configured()
        price_id = self._price_id(plan_id)
        params: Dict[str, Any] = {
            "mode": "subscription",
            "payment_method_collection": "always",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": f"{self.app_url}/settings/plan?checkout=success",
            "cancel_url": f"{self.app_url}/pricing?checkout=cancelled",
            "client_reference_id": user_id,
            "metadata": {"user_id": user_id, "plan_id": plan_id},
            "subscription_data": {
                "trial_period_days": self.trial_days,
                "metadata": {"user_id": user_id, "plan_id": plan_id},
            },
        }
        if user_email:
            params["customer_email"] = user_email
        if self.coupon_id:
            params["discounts"] = [{"coupon": self.coupon_id}]
        else:
            params["allow_promotion_codes"] = True
        session = self.stripe.checkout.Session.create(**params)
        return {
            "checkout_session_id": session.get("id"),
            "url": session.get("url"),
            "plan_id": plan_id,
        }

    def create_portal_session(self, *, user_id: str) -> Dict[str, Any]:
        self._require_configured()
        subscription = self.repository.get_user_subscription(user_id)
        if not subscription or not subscription.stripe_customer_id:
            raise ValueError("stripe_customer_not_found")
        session = self.stripe.billing_portal.Session.create(
            customer=subscription.stripe_customer_id,
            return_url=f"{self.app_url}/settings/plan",
        )
        return {"url": session.get("url")}

    def construct_event(self, payload: bytes, signature: str):
        self._require_configured()
        if not self.webhook_secret:
            raise StripeBillingConfigError("STRIPE_WEBHOOK_SECRET_missing")
        return self.stripe.Webhook.construct_event(payload, signature, self.webhook_secret)

    def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(event.get("type") or "")
        obj = (event.get("data") or {}).get("object") or {}
        if event_type == "checkout.session.completed":
            return self._handle_checkout_completed(obj)
        if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
            return self._handle_subscription_updated(obj)
        if event_type == "customer.subscription.deleted":
            return self._handle_subscription_deleted(obj)
        if event_type == "invoice.payment_succeeded":
            return self._handle_invoice_payment_succeeded(obj)
        if event_type == "invoice.payment_failed":
            return self._handle_invoice_payment_failed(obj)
        return {"handled": False, "event_type": event_type}

    def _plan_from_price(self, price_id: str, fallback: str = "starter") -> str:
        for plan_id, configured in self.price_ids.items():
            if configured and configured == price_id:
                return plan_id
        return fallback

    def _subscription_payload(self, subscription: Dict[str, Any], *, user_id: Optional[str] = None) -> Dict[str, Any]:
        metadata = subscription.get("metadata") or {}
        plan_id = str(metadata.get("plan_id") or "starter")
        items = ((subscription.get("items") or {}).get("data") or [])
        price_id = ""
        if items:
            price_id = (((items[0] or {}).get("price") or {}).get("id") or "")
            plan_id = self._plan_from_price(price_id, plan_id)
        status = str(subscription.get("status") or "active")
        trial_end = _ts(subscription.get("trial_end"))
        trial_status = "active" if status == "trialing" else ("converted" if status == "active" else None)
        billing_status = "current" if status in {"active", "trialing"} else status
        plan = PLAN_DEFINITIONS.get(plan_id, PLAN_DEFINITIONS["starter"])
        return {
            "user_id": user_id or str(metadata.get("user_id") or ""),
            "plan_id": plan_id,
            "status": status,
            "trial_start_at": _ts(subscription.get("trial_start")),
            "trial_end_at": trial_end,
            "trial_status": trial_status,
            "auto_renew": not bool(subscription.get("cancel_at_period_end")),
            "current_period_start": _ts(subscription.get("current_period_start")),
            "current_period_end": _ts(subscription.get("current_period_end")),
            "billing_status": billing_status,
            "stripe_customer_id": str(subscription.get("customer") or ""),
            "stripe_subscription_id": str(subscription.get("id") or ""),
            "stripe_price_id": price_id,
            "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
            "plan_price_effective": plan.price_monthly_usd,
            "metadata": {"stripe_status": status, **metadata},
        }

    def _handle_checkout_completed(self, session: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(session.get("client_reference_id") or (session.get("metadata") or {}).get("user_id") or "")
        plan_id = str((session.get("metadata") or {}).get("plan_id") or "starter")
        data = {
            "user_id": user_id,
            "plan_id": plan_id,
            "status": "trialing",
            "trial_status": "active",
            "billing_status": "checkout_completed",
            "stripe_customer_id": str(session.get("customer") or ""),
            "stripe_subscription_id": str(session.get("subscription") or ""),
            "stripe_checkout_session_id": str(session.get("id") or ""),
            "metadata": {"checkout_session_id": session.get("id"), "plan_id": plan_id},
        }
        self.repository.upsert_user_subscription(data)
        return {"handled": True, "event_type": "checkout.session.completed", "user_id": user_id}

    def _handle_subscription_updated(self, subscription: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.repository.get_user_subscription_by_stripe_subscription(str(subscription.get("id") or ""))
        payload = self._subscription_payload(subscription, user_id=existing.user_id if existing else None)
        if not payload.get("user_id"):
            payload["user_id"] = str((subscription.get("metadata") or {}).get("user_id") or "")
        self.repository.upsert_user_subscription(payload)
        return {"handled": True, "event_type": "customer.subscription.updated", "user_id": payload.get("user_id")}

    def _handle_subscription_deleted(self, subscription: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.repository.get_user_subscription_by_stripe_subscription(str(subscription.get("id") or ""))
        if existing:
            self.repository.update_user_subscription(
                existing.id,
                {"status": "canceled", "billing_status": "canceled", "trial_status": "canceled", "auto_renew": False},
            )
        return {"handled": True, "event_type": "customer.subscription.deleted", "user_id": existing.user_id if existing else None}

    def _handle_invoice_payment_succeeded(self, invoice: Dict[str, Any]) -> Dict[str, Any]:
        stripe_subscription_id = str(invoice.get("subscription") or "")
        existing = self.repository.get_user_subscription_by_stripe_subscription(stripe_subscription_id)
        if existing:
            self.repository.update_user_subscription(
                existing.id,
                {
                    "status": "active",
                    "billing_status": "current",
                    "trial_status": "converted",
                    "payment_failure_at": None,
                    "grace_period_end_at": None,
                    "billing_cycle_count": int(existing.billing_cycle_count or 0) + 1,
                },
            )
        return {"handled": True, "event_type": "invoice.payment_succeeded", "user_id": existing.user_id if existing else None}

    def _handle_invoice_payment_failed(self, invoice: Dict[str, Any]) -> Dict[str, Any]:
        stripe_subscription_id = str(invoice.get("subscription") or "")
        existing = self.repository.get_user_subscription_by_stripe_subscription(stripe_subscription_id)
        if existing:
            self.repository.update_user_subscription(
                existing.id,
                {
                    "status": "past_due",
                    "billing_status": "past_due",
                    "payment_failure_at": _now(),
                    "grace_period_end_at": (datetime.now(timezone.utc) + timedelta(days=self.grace_days)).isoformat(),
                },
            )
        return {"handled": True, "event_type": "invoice.payment_failed", "user_id": existing.user_id if existing else None}
