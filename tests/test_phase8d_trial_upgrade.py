import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from billing import BillingService, InMemoryBillingRepository
from billing.models import SubscriptionRecord


JWT_SECRET = "phase8d-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _service_with_subscription(subscription: SubscriptionRecord) -> tuple[BillingService, InMemoryBillingRepository]:
    repo = InMemoryBillingRepository(default_plan_id="trial")
    repo.set_user_subscription(subscription)
    return BillingService(repo), repo


def _client(service: BillingService) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.billing_service = service
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_trial_countdown():
    now = datetime.now(timezone.utc)
    service, _ = _service_with_subscription(
        SubscriptionRecord(
            id="sub-1",
            user_id="user-1",
            plan_id="trial",
            status="trialing",
            trial_status="active",
            trial_start_at=now.isoformat(),
            trial_end_at=(now + timedelta(days=12)).isoformat(),
        )
    )

    assert service.is_trial_active("user-1") is True
    assert service.get_trial_days_remaining("user-1") == 12
    assert service.is_trial_expired("user-1") is False


def test_trial_expiry_auto_converts_to_starter():
    now = datetime.now(timezone.utc)
    service, repo = _service_with_subscription(
        SubscriptionRecord(
            id="sub-1",
            user_id="user-1",
            plan_id="trial",
            status="trialing",
            trial_status="active",
            auto_renew=True,
            trial_start_at=(now - timedelta(days=31)).isoformat(),
            trial_end_at=(now - timedelta(days=1)).isoformat(),
        )
    )

    plan = service.get_user_plan("user-1")

    assert plan.id == "starter"
    assert repo.subscriptions["user-1"].trial_status == "converted"
    assert repo.subscriptions["user-1"].billing_status == "trial_converted"
    assert repo.subscriptions["user-1"].plan_price_effective == 14.99
    assert repo.subscriptions["user-1"].billing_cycle_count == 0
    assert repo.credit_entries[-1].event_type == "trial_auto_converted"


def test_cancel_before_expiry_does_not_convert():
    now = datetime.now(timezone.utc)
    service, repo = _service_with_subscription(
        SubscriptionRecord(
            id="sub-1",
            user_id="user-1",
            plan_id="trial",
            status="trialing",
            trial_status="active",
            auto_renew=False,
            trial_start_at=(now - timedelta(days=31)).isoformat(),
            trial_end_at=(now - timedelta(days=1)).isoformat(),
        )
    )

    service.get_user_plan("user-1")

    assert repo.subscriptions["user-1"].status == "canceled"
    assert repo.subscriptions["user-1"].trial_status == "expired"
    assert repo.credit_entries == []


def test_upgrade_prompt_for_trial_expiring():
    now = datetime.now(timezone.utc)
    service, _ = _service_with_subscription(
        SubscriptionRecord(
            id="sub-1",
            user_id="user-1",
            plan_id="trial",
            status="trialing",
            trial_status="active",
            trial_end_at=(now + timedelta(days=4)).isoformat(),
        )
    )

    hint = service.upgrade_status("user-1")["suggested_upgrade"]

    assert hint["show"] is True
    assert hint["type"] == "trial_expiring"
    assert hint["target_plan"] == "Starter"


def test_upgrade_prompt_for_low_credits():
    repo = InMemoryBillingRepository(default_plan_id="starter")
    service = BillingService(repo)
    service.deduct_credits("user-1", "studio_agent_planner", amount=1855)

    hint = service.upgrade_status("user-1")["suggested_upgrade"]

    assert hint["show"] is True
    assert hint["type"] == "low_credits"
    assert hint["target_plan"] == "Pro"


def test_discount_applied_for_first_three_cycles():
    repo = InMemoryBillingRepository(default_plan_id="pro")
    repo.set_user_subscription(
        SubscriptionRecord(
            id="sub-pro",
            user_id="user-1",
            plan_id="pro",
            status="active",
            trial_status="converted",
            billing_cycle_count=2,
        )
    )
    service = BillingService(repo)

    summary = service.plan_summary("user-1")

    assert summary["base_price"] == 39.99
    assert summary["discounted_price"] == 29.99
    assert summary["price_monthly"] == 29.99
    assert summary["discount_months_remaining"] == 1


def test_discount_expires_after_three_cycles():
    repo = InMemoryBillingRepository(default_plan_id="pro")
    repo.set_user_subscription(
        SubscriptionRecord(
            id="sub-pro",
            user_id="user-1",
            plan_id="pro",
            status="active",
            trial_status="converted",
            billing_cycle_count=3,
        )
    )
    service = BillingService(repo)

    summary = service.plan_summary("user-1")

    assert summary["discounted_price"] is None
    assert summary["price_monthly"] == 39.99
    assert summary["discount_months_remaining"] == 0


def test_upgrade_status_endpoint():
    now = datetime.now(timezone.utc)
    service, _ = _service_with_subscription(
        SubscriptionRecord(
            id="sub-1",
            user_id="user-1",
            plan_id="trial",
            status="trialing",
            trial_status="active",
            trial_end_at=(now + timedelta(days=3)).isoformat(),
        )
    )
    client = _client(service)

    response = client.get("/settings/upgrade-status", headers=_auth())

    assert response.status_code == 200
    body = response.json()["upgrade_status"]
    assert body["days_remaining"] == 3
    assert body["suggested_upgrade"]["type"] == "trial_expiring"
