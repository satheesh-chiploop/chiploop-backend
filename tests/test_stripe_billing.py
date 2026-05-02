import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billing import BillingPaymentRequired, BillingService, InMemoryBillingRepository
from billing.models import SubscriptionRecord
from stripe_billing import StripeBillingService


class _FakeCheckoutSession:
    @staticmethod
    def create(**kwargs):
        _FakeStripe.last_checkout = kwargs
        return {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}


class _FakePortalSession:
    @staticmethod
    def create(**kwargs):
        _FakeStripe.last_portal = kwargs
        return {"url": "https://billing.stripe.test/session"}


class _FakeWebhook:
    @staticmethod
    def construct_event(payload, signature, secret):
        return {"type": "noop", "data": {"object": {}}, "signature": signature, "secret": secret}


class _FakeStripe:
    api_key = None
    last_checkout = None
    last_portal = None

    class checkout:
        Session = _FakeCheckoutSession

    class billing_portal:
        Session = _FakePortalSession

    Webhook = _FakeWebhook


@pytest.fixture(autouse=True)
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_starter")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_PRO_MAX", "price_pro_max")
    monkeypatch.setenv("CHIPLOOP_APP_URL", "https://chiploop.test")
    yield


def test_checkout_session_uses_hosted_stripe_and_trial():
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_checkout_session(user_id="user-1", user_email="u@example.com", plan_id="starter")

    assert result["url"].startswith("https://checkout.stripe.test")
    assert _FakeStripe.last_checkout["mode"] == "subscription"
    assert _FakeStripe.last_checkout["payment_method_collection"] == "always"
    assert _FakeStripe.last_checkout["subscription_data"]["trial_period_days"] == 7
    assert _FakeStripe.last_checkout["client_reference_id"] == "user-1"


def test_customer_portal_requires_existing_stripe_customer():
    repo = InMemoryBillingRepository()
    repo.set_user_subscription(
        SubscriptionRecord(user_id="user-1", plan_id="starter", stripe_customer_id="cus_123")
    )
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_portal_session(user_id="user-1")

    assert result["url"].startswith("https://billing.stripe.test")
    assert _FakeStripe.last_portal["customer"] == "cus_123"


def test_checkout_completed_webhook_creates_trial_subscription():
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    service.handle_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_123",
            "client_reference_id": "user-1",
            "customer": "cus_123",
            "subscription": "sub_123",
            "metadata": {"plan_id": "starter"},
        }},
    })

    subscription = repo.get_user_subscription("user-1")
    assert subscription is not None
    assert subscription.stripe_customer_id == "cus_123"
    assert subscription.stripe_subscription_id == "sub_123"
    assert subscription.status == "trialing"


def test_payment_failure_blocks_after_grace_period():
    repo = InMemoryBillingRepository(default_plan_id="pro")
    repo.set_user_subscription(
        SubscriptionRecord(
            id="sub-row",
            user_id="user-1",
            plan_id="pro",
            status="active",
            billing_status="current",
            stripe_subscription_id="sub_123",
        )
    )
    service = StripeBillingService(repo, stripe_module=_FakeStripe)
    service.handle_event({"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_123"}}})

    subscription = repo.get_user_subscription("user-1")
    assert subscription.billing_status == "past_due"
    subscription.grace_period_end_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    repo.set_user_subscription(subscription)

    with pytest.raises(BillingPaymentRequired):
        BillingService(repo).deduct_credits("user-1", "studio_agent_planner")
