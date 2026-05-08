import os
import re
from typing import Any, Dict, List, Optional

from stripe_billing import StripeBillingConfigError

from .models import WorkshopRegistration, utcnow_iso
from .repository import WorkshopRegistrationRepository


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WORKSHOP_PRICE_USD = 99.98
WORKSHOP_BATCH_CAPACITY = 10
WORKSHOP_BATCHES = [
    {
        "id": "2026-06-06_09-30am_pt",
        "label": "June 6 + June 13, 2026, 9:30 AM PST",
        "day_1": "2026-06-06",
        "day_2": "2026-06-13",
        "time": "9:30 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-06_09-30pm_pt",
        "label": "June 6 + June 13, 2026, 9:30 PM PST",
        "day_1": "2026-06-06",
        "day_2": "2026-06-13",
        "time": "9:30 PM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-20_09-30am_pt",
        "label": "June 20 + June 27, 2026, 9:30 AM PST",
        "day_1": "2026-06-20",
        "day_2": "2026-06-27",
        "time": "9:30 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-20_09-30pm_pt",
        "label": "June 20 + June 27, 2026, 9:30 PM PST",
        "day_1": "2026-06-20",
        "day_2": "2026-06-27",
        "time": "9:30 PM",
        "timezone": "PST",
    },
]
WORKSHOP_BATCH_IDS = {batch["id"] for batch in WORKSHOP_BATCHES}


class WorkshopRegistrationError(ValueError):
    pass


class WorkshopService:
    def __init__(self, repository: WorkshopRegistrationRepository, stripe_module=None):
        self.repository = repository
        self.stripe = stripe_module or self._load_stripe()
        self.secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        self.price_id = os.environ.get("STRIPE_PRICE_WORKSHOP_2DAY", "").strip()
        self.app_url = os.environ.get("CHIPLOOP_APP_URL", os.environ.get("FRONTEND_URL", "http://localhost:3000")).rstrip("/")
        if self.secret_key:
            self.stripe.api_key = self.secret_key

    def _load_stripe(self):
        try:
            import stripe  # type: ignore
        except ImportError as exc:
            raise StripeBillingConfigError("stripe_package_missing") from exc
        return stripe

    def _require_stripe_configured(self) -> None:
        if not self.secret_key:
            raise StripeBillingConfigError("STRIPE_SECRET_KEY_missing")
        if not self.price_id:
            raise StripeBillingConfigError("STRIPE_PRICE_WORKSHOP_2DAY_missing")
        if self.price_id.startswith("prod_"):
            raise StripeBillingConfigError("STRIPE_PRICE_WORKSHOP_2DAY_uses_product_id_expected_price_id")
        if not self.price_id.startswith("price_"):
            raise StripeBillingConfigError("STRIPE_PRICE_WORKSHOP_2DAY_invalid_expected_price_id")

    def batches(self) -> List[Dict[str, Any]]:
        batches = []
        for batch in WORKSHOP_BATCHES:
            booked = self.repository.count_paid_by_batch(str(batch["id"]))
            batches.append({
                **batch,
                "capacity": WORKSHOP_BATCH_CAPACITY,
                "booked": booked,
                "remaining": max(WORKSHOP_BATCH_CAPACITY - booked, 0),
                "full": booked >= WORKSHOP_BATCH_CAPACITY,
                "price_usd": WORKSHOP_PRICE_USD,
            })
        return batches

    def create_checkout(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_stripe_configured()
        registration = self._pending_registration(payload)
        metadata = {
            "checkout_kind": "workshop",
            "registration_id": registration.id,
            "batch_id": registration.batch_id,
        }
        session = self.stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": self.price_id, "quantity": 1}],
            success_url=f"{self.app_url}/workshop?payment=success&registration_id={registration.id}",
            cancel_url=f"{self.app_url}/workshop?payment=cancelled&registration_id={registration.id}",
            customer_email=registration.email,
            client_reference_id=registration.id,
            metadata=metadata,
            payment_intent_data={"metadata": metadata},
        )
        registration.stripe_checkout_session_id = str(session.get("id") or "")
        registration.updated_at = utcnow_iso()
        self.repository.save(registration)
        return {
            "registration_id": registration.id,
            "checkout_session_id": registration.stripe_checkout_session_id,
            "url": session.get("url"),
        }

    def _pending_registration(self, payload: Dict[str, Any]) -> WorkshopRegistration:
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        batch_id = str(payload.get("batch_id") or "").strip()
        if len(name) < 2:
            raise WorkshopRegistrationError("name_required")
        if not EMAIL_RE.match(email):
            raise WorkshopRegistrationError("valid_email_required")
        if batch_id not in WORKSHOP_BATCH_IDS:
            raise WorkshopRegistrationError("batch_required")
        if self.repository.count_paid_by_batch(batch_id) >= WORKSHOP_BATCH_CAPACITY:
            raise WorkshopRegistrationError("batch_full")
        registration = WorkshopRegistration(
            name=name[:160],
            email=email[:254],
            company=str(payload.get("company") or "").strip()[:160],
            role=str(payload.get("role") or "").strip()[:120],
            loop_interest=str(payload.get("loop_interest") or "").strip()[:120],
            batch_id=batch_id,
            questions=str(payload.get("questions") or "").strip()[:1000],
            source=str(payload.get("source") or "workshop_page").strip()[:80],
            status="pending_payment",
        )
        return self.repository.save(registration)

    def complete_checkout(self, session: Dict[str, Any]) -> Dict[str, Any]:
        metadata = session.get("metadata") or {}
        registration_id = str(metadata.get("registration_id") or session.get("client_reference_id") or "")
        registration = self.repository.get(registration_id) if registration_id else None
        if not registration:
            checkout_id = str(session.get("id") or "")
            registration = self.repository.get_by_checkout_session(checkout_id) if checkout_id else None
        if not registration:
            return {"handled": False, "event_type": "checkout.session.completed", "reason": "workshop_registration_not_found"}
        registration.status = "paid"
        registration.stripe_checkout_session_id = str(session.get("id") or registration.stripe_checkout_session_id or "")
        registration.stripe_customer_id = str(session.get("customer") or registration.stripe_customer_id or "")
        registration.stripe_payment_intent_id = str(session.get("payment_intent") or registration.stripe_payment_intent_id or "")
        registration.paid_at = utcnow_iso()
        registration.updated_at = registration.paid_at
        self.repository.save(registration)
        return {
            "handled": True,
            "event_type": "checkout.session.completed",
            "registration_id": registration.id,
            "checkout_kind": "workshop",
        }

    def get_registration(self, registration_id: str) -> Optional[WorkshopRegistration]:
        return self.repository.get(registration_id)
