import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_routes
from billing import InMemoryBillingRepository
from stripe_billing import StripeBillingService
from workshop import InMemoryWorkshopRegistrationRepository, WorkshopService


class _FakeCheckoutSession:
    @staticmethod
    def create(**kwargs):
        _FakeStripe.last_checkout = kwargs
        return {"id": "cs_workshop_123", "url": "https://checkout.stripe.test/workshop"}


class _FakeWebhook:
    @staticmethod
    def construct_event(payload, signature, secret):
        return {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_workshop_123",
                    "customer": "cus_123",
                    "payment_intent": "pi_123",
                    "metadata": {
                        "checkout_kind": "workshop",
                        "registration_id": _FakeStripe.registration_id,
                        "batch_id": "2026-06-06_09-30am_pt",
                    },
                }
            },
        }


class _FakeStripe:
    api_key = None
    last_checkout = None
    registration_id = ""

    class checkout:
        Session = _FakeCheckoutSession

    Webhook = _FakeWebhook


def _client(repo: InMemoryWorkshopRegistrationRepository | None = None) -> tuple[TestClient, InMemoryWorkshopRegistrationRepository]:
    repository = repo or InMemoryWorkshopRegistrationRepository()
    app = FastAPI()
    app.state.stripe_billing_service = StripeBillingService(InMemoryBillingRepository(), stripe_module=_FakeStripe)
    app.state.workshop_service = WorkshopService(repository, stripe_module=_FakeStripe)
    app.include_router(browser_routes.router)
    return TestClient(app), repository


def test_workshop_batches_show_capacity(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_WORKSHOP_2DAY", "price_workshop")
    client, _ = _client()

    response = client.get("/workshop/batches")

    assert response.status_code == 200
    first = response.json()["batches"][0]
    assert first["capacity"] == 10
    assert first["remaining"] == 10
    assert first["price_usd"] == 149
    assert first["id"] == "2026-06-06_09-30am_pt"


def test_workshop_checkout_creates_pending_registration(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_WORKSHOP_2DAY", "price_workshop")
    monkeypatch.setenv("CHIPLOOP_APP_URL", "https://chiploop.test")
    client, repo = _client()

    response = client.post(
        "/workshop/checkout",
        json={
            "name": "Grace Hopper",
            "email": "Grace@Example.com",
            "batch_id": "2026-06-06_09-30am_pt",
            "loop_interest": "Digital",
        },
    )

    assert response.status_code == 200
    registration_id = response.json()["registration_id"]
    registration = repo.records[registration_id]
    assert registration.email == "grace@example.com"
    assert registration.status == "pending_payment"
    assert registration.stripe_checkout_session_id == "cs_workshop_123"
    assert _FakeStripe.last_checkout["mode"] == "payment"
    assert _FakeStripe.last_checkout["line_items"] == [{"price": "price_workshop", "quantity": 1}]
    assert _FakeStripe.last_checkout["metadata"]["checkout_kind"] == "workshop"
    assert "registration_id=" in _FakeStripe.last_checkout["success_url"]


def test_workshop_checkout_requires_price_id(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("STRIPE_PRICE_WORKSHOP_2DAY", raising=False)
    client, _ = _client()

    response = client.post(
        "/workshop/checkout",
        json={"name": "User", "email": "user@example.com", "batch_id": "2026-06-06_09-30am_pt"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "STRIPE_PRICE_WORKSHOP_2DAY_missing"


def test_workshop_webhook_marks_registration_paid(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_WORKSHOP_2DAY", "price_workshop")
    client, repo = _client()
    checkout = client.post(
        "/workshop/checkout",
        json={"name": "Paid User", "email": "paid@example.com", "batch_id": "2026-06-06_09-30am_pt"},
    )
    registration_id = checkout.json()["registration_id"]
    _FakeStripe.registration_id = registration_id

    response = client.post("/billing/stripe/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    assert response.status_code == 200
    assert repo.records[registration_id].status == "paid"
    assert repo.records[registration_id].stripe_customer_id == "cus_123"
    assert repo.records[registration_id].stripe_payment_intent_id == "pi_123"


def test_workshop_full_batch_rejected(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_WORKSHOP_2DAY", "price_workshop")
    client, _ = _client()

    for index in range(10):
        checkout = client.post(
            "/workshop/checkout",
            json={
                "name": f"User {index}",
                "email": f"user{index}@example.com",
                "batch_id": "2026-06-06_09-30am_pt",
            },
        )
        _FakeStripe.registration_id = checkout.json()["registration_id"]
        client.post("/billing/stripe/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    response = client.post(
        "/workshop/checkout",
        json={"name": "Late User", "email": "late@example.com", "batch_id": "2026-06-06_09-30am_pt"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "batch_full"
