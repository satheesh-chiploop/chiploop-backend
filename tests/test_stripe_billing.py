import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billing import BillingPaymentRequired, BillingService, InMemoryBillingRepository
from billing.models import SubscriptionRecord
from stripe_billing import StripeBillingConfigError, StripeBillingService


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
    last_subscription_modify = None
    subscription = {
        "id": "sub_123",
        "customer": "cus_123",
        "status": "active",
        "metadata": {"user_id": "user-1", "plan_id": "starter"},
        "items": {"data": [{"id": "si_base", "price": {"id": "price_starter"}}]},
    }

    class checkout:
        Session = _FakeCheckoutSession

    class billing_portal:
        Session = _FakePortalSession

    class Subscription:
        @staticmethod
        def retrieve(subscription_id):
            return {**_FakeStripe.subscription, "id": subscription_id}

        @staticmethod
        def modify(subscription_id, **kwargs):
            _FakeStripe.last_subscription_modify = {"subscription_id": subscription_id, **kwargs}
            price_id = kwargs["items"][0]["price"]
            return {
                **_FakeStripe.subscription,
                "id": subscription_id,
                "metadata": kwargs.get("metadata") or {},
                "items": {"data": [{"id": "si_base", "price": {"id": price_id}}]},
            }

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


def test_paid_checkout_session_uses_hosted_stripe_without_trial():
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_checkout_session(user_id="user-1", user_email="u@example.com", plan_id="starter")

    assert result["url"].startswith("https://checkout.stripe.test")
    assert result["checkout_kind"] == "paid"
    assert _FakeStripe.last_checkout["mode"] == "subscription"
    assert _FakeStripe.last_checkout["payment_method_collection"] == "always"
    assert "trial_period_days" not in _FakeStripe.last_checkout["subscription_data"]
    assert _FakeStripe.last_checkout["client_reference_id"] == "user-1"
    assert _FakeStripe.last_checkout["metadata"]["checkout_kind"] == "paid"
    assert _FakeStripe.last_checkout["allow_promotion_codes"] is True
    assert "discounts" not in _FakeStripe.last_checkout


def test_trial_checkout_session_uses_starter_trial():
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_checkout_session(
        user_id="user-1",
        user_email="u@example.com",
        plan_id="starter",
        trial=True,
    )

    assert result["checkout_kind"] == "trial"
    assert _FakeStripe.last_checkout["line_items"] == [{"price": "price_starter", "quantity": 1}]
    assert _FakeStripe.last_checkout["subscription_data"]["trial_period_days"] == 3
    assert _FakeStripe.last_checkout["metadata"]["checkout_kind"] == "trial"


def test_checkout_session_uses_intro_coupon_without_promotion_codes(monkeypatch):
    monkeypatch.setenv("STRIPE_INTRO_COUPON_ID", "coupon_live_intro")
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    service.create_checkout_session(user_id="user-1", user_email="u@example.com", plan_id="starter")

    assert _FakeStripe.last_checkout["discounts"] == [{"coupon": "coupon_live_intro"}]
    assert "allow_promotion_codes" not in _FakeStripe.last_checkout


def test_checkout_session_uses_plan_specific_intro_coupon(monkeypatch):
    monkeypatch.setenv("STRIPE_INTRO_COUPON_ID", "coupon_shared")
    monkeypatch.setenv("STRIPE_INTRO_COUPON_PRO", "coupon_pro")
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    service.create_checkout_session(user_id="user-1", user_email="u@example.com", plan_id="pro")

    assert _FakeStripe.last_checkout["discounts"] == [{"coupon": "coupon_pro"}]


def test_loop_addon_checkout_uses_configured_price(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_LOOP_DIGITAL_DESIGN_ADVANCED", "price_loop_advanced")
    repo = InMemoryBillingRepository()
    repo.set_user_subscription(
        SubscriptionRecord(user_id="user-1", plan_id="starter", stripe_customer_id="cus_123")
    )
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_loop_addon_checkout_session(
        user_id="user-1",
        user_email="u@example.com",
        loop_key="digital_design",
        addon_type="add_advanced",
    )

    assert result["checkout_kind"] == "loop_addon"
    assert _FakeStripe.last_checkout["mode"] == "subscription"
    assert _FakeStripe.last_checkout["customer"] == "cus_123"
    assert _FakeStripe.last_checkout["line_items"] == [{"price": "price_loop_advanced", "quantity": 1}]
    assert _FakeStripe.last_checkout["metadata"]["loop_key"] == "digital_design"
    assert _FakeStripe.last_checkout["metadata"]["loop_level"] == "advanced"


def test_credit_pack_checkout_and_webhook_grants_credits(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_CREDITS_500", "price_credits_500")
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_credit_checkout_session(
        user_id="user-1",
        user_email="u@example.com",
        credits=500,
    )

    assert result["checkout_kind"] == "credit_pack"
    assert _FakeStripe.last_checkout["mode"] == "payment"
    assert _FakeStripe.last_checkout["line_items"] == [{"price": "price_credits_500", "quantity": 1}]

    service.handle_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_credits",
            "client_reference_id": "user-1",
            "customer": "cus_123",
            "payment_intent": "pi_123",
            "metadata": {"checkout_kind": "credit_pack", "credits": "500"},
        }},
    })

    assert BillingService(repo).get_credit_balance("user-1")["monthly_credits"] == 600


def test_checkout_rejects_product_id_env(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "prod_starter")
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    with pytest.raises(StripeBillingConfigError, match="expected_price_id"):
        service.create_checkout_session(user_id="user-1", user_email="u@example.com", plan_id="starter")


def test_customer_portal_requires_existing_stripe_customer():
    repo = InMemoryBillingRepository()
    repo.set_user_subscription(
        SubscriptionRecord(user_id="user-1", plan_id="starter", stripe_customer_id="cus_123")
    )
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.create_portal_session(user_id="user-1")

    assert result["url"].startswith("https://billing.stripe.test")
    assert _FakeStripe.last_portal["customer"] == "cus_123"


def test_change_plan_updates_existing_subscription_item():
    repo = InMemoryBillingRepository()
    repo.set_user_subscription(
        SubscriptionRecord(
            id="row-1",
            user_id="user-1",
            plan_id="starter",
            stripe_customer_id="cus_123",
            stripe_subscription_id="sub_123",
            stripe_price_id="price_starter",
        )
    )
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    result = service.change_plan(user_id="user-1", plan_id="pro")

    assert result["changed"] is True
    assert _FakeStripe.last_subscription_modify["subscription_id"] == "sub_123"
    assert _FakeStripe.last_subscription_modify["items"] == [{"id": "si_base", "price": "price_pro"}]
    assert _FakeStripe.last_subscription_modify["proration_behavior"] == "create_prorations"
    subscription = repo.get_user_subscription("user-1")
    assert subscription.plan_id == "pro"
    assert subscription.stripe_price_id == "price_pro"


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
            "metadata": {"plan_id": "starter", "checkout_kind": "trial"},
        }},
    })

    subscription = repo.get_user_subscription("user-1")
    assert subscription is not None
    assert subscription.stripe_customer_id == "cus_123"
    assert subscription.stripe_subscription_id == "sub_123"
    assert subscription.status == "trialing"


def test_checkout_completed_webhook_creates_paid_subscription():
    repo = InMemoryBillingRepository()
    service = StripeBillingService(repo, stripe_module=_FakeStripe)

    service.handle_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_123",
            "client_reference_id": "user-1",
            "customer": "cus_123",
            "subscription": "sub_123",
            "metadata": {"plan_id": "pro", "checkout_kind": "paid"},
        }},
    })

    subscription = repo.get_user_subscription("user-1")
    assert subscription is not None
    assert subscription.plan_id == "pro"
    assert subscription.status == "active"
    assert subscription.billing_status == "current"


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
