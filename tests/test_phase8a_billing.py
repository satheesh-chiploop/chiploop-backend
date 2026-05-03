import copy
import sys
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_auth
import browser_routes
from billing import BillingService, CreditLimitExceeded, EntitlementDenied, InMemoryBillingRepository, TrialCheckoutRequired
from billing.entitlements import PLAN_DEFINITIONS


JWT_SECRET = "phase8a-test-secret"


def _token(user_id: str = "user-1") -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def _auth(user_id: str = "user-1") -> dict:
    return {"Authorization": f"Bearer {_token(user_id)}"}


def _client(billing: BillingService) -> TestClient:
    browser_auth.SUPABASE_JWT_SECRET = JWT_SECRET
    app = FastAPI()
    app.state.billing_service = billing
    app.include_router(browser_routes.router)
    return TestClient(app)


def test_trial_plan_fallback():
    service = BillingService(InMemoryBillingRepository())

    plan = service.get_user_plan("missing-user")

    assert plan.id == "trial"
    assert service.get_entitlements("missing-user").monthly_credits == 100


def test_account_without_checkout_cannot_run_paid_actions():
    service = BillingService(InMemoryBillingRepository(default_plan_id="account"))

    plan = service.get_user_plan("missing-user")
    summary = service.plan_summary("missing-user")

    assert plan.id == "account"
    assert summary["requires_checkout"] is True
    assert summary["can_run_workflows"] is False
    with pytest.raises(TrialCheckoutRequired):
        service.assert_checkout_started("missing-user")


def test_entitlement_allowed_and_denied():
    repo = InMemoryBillingRepository(default_plan_id="trial")
    service = BillingService(repo)

    service.assert_entitlement("user-1", "agent_planner_enabled")
    with pytest.raises(EntitlementDenied):
        service.assert_entitlement("user-1", "dag_optimization_enabled")

    repo.set_user_plan("user-1", "pro")
    service.assert_entitlement("user-1", "dag_optimization_enabled")


def test_credit_deduction_updates_balance():
    service = BillingService(InMemoryBillingRepository(default_plan_id="starter"))

    result = service.deduct_credits("user-1", "studio_agent_planner")

    assert result["deducted"] == 5
    assert result["credits_remaining"] == 1995
    assert service.get_credit_balance("user-1")["credits_used"] == 5


def test_insufficient_credits():
    repo = InMemoryBillingRepository(default_plan_id="trial")
    service = BillingService(repo)
    service.deduct_credits("user-1", "private_agent_save", amount=100)

    with pytest.raises(CreditLimitExceeded):
        service.deduct_credits("user-1", "studio_agent_planner")


def test_api_key_limit_enforced():
    repo = InMemoryBillingRepository(default_plan_id="trial")
    repo.api_key_counts["user-1"] = 1
    service = BillingService(repo)

    with pytest.raises(EntitlementDenied):
        service.assert_api_key_limit("user-1")


def test_sdk_cli_available_only_on_pro_and_above():
    repo = InMemoryBillingRepository(default_plan_id="starter")
    service = BillingService(repo)

    with pytest.raises(EntitlementDenied):
        service.assert_entitlement("user-1", "sdk_cli_enabled")

    repo.set_user_plan("user-1", "pro")
    service.assert_entitlement("user-1", "sdk_cli_enabled")

    repo.set_user_plan("user-1", "pro_max")
    service.assert_entitlement("user-1", "sdk_cli_enabled")

    repo.set_user_plan("user-1", "enterprise")
    service.assert_entitlement("user-1", "sdk_cli_enabled")


def test_agent_factory_entitlement_denied_for_custom_plan():
    class NoFactoryRepository(InMemoryBillingRepository):
        def get_plan(self, plan_id):
            plan = copy.deepcopy(PLAN_DEFINITIONS["trial"])
            plan.entitlements.agent_factory_dry_run_enabled = False
            return plan

    service = BillingService(NoFactoryRepository(default_plan_id="trial"))

    with pytest.raises(EntitlementDenied):
        service.assert_entitlement("user-1", "agent_factory_dry_run_enabled")


def test_dag_entitlement_free_denied_pro_allowed():
    repo = InMemoryBillingRepository(default_plan_id="trial")
    service = BillingService(repo)

    with pytest.raises(EntitlementDenied):
        service.assert_entitlement("user-1", "dag_optimization_enabled")

    repo.set_user_plan("user-1", "pro")
    service.assert_entitlement("user-1", "dag_optimization_enabled")


def test_settings_plan_endpoint():
    billing = BillingService(InMemoryBillingRepository(default_plan_id="starter"))
    client = _client(billing)

    response = client.get("/settings/plan", headers=_auth())

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["current_plan"]["id"] == "starter"
    assert plan["monthly_credits"] == 2000
    assert plan["credits_remaining"] == 2000
    assert plan["billing_status"] == "placeholder"
