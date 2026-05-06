from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkshopRegistration:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    email: str = ""
    company: str = ""
    role: str = ""
    loop_interest: str = ""
    batch_id: str = ""
    questions: str = ""
    source: str = "workshop_page"
    status: str = "pending_payment"
    stripe_checkout_session_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    stripe_payment_intent_id: Optional[str] = None
    paid_at: Optional[str] = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkshopRegistration":
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or ""),
            email=str(data.get("email") or ""),
            company=str(data.get("company") or ""),
            role=str(data.get("role") or ""),
            loop_interest=str(data.get("loop_interest") or ""),
            batch_id=str(data.get("batch_id") or ""),
            questions=str(data.get("questions") or ""),
            source=str(data.get("source") or "workshop_page"),
            status=str(data.get("status") or "pending_payment"),
            stripe_checkout_session_id=data.get("stripe_checkout_session_id"),
            stripe_customer_id=data.get("stripe_customer_id"),
            stripe_payment_intent_id=data.get("stripe_payment_intent_id"),
            paid_at=data.get("paid_at"),
            created_at=str(data.get("created_at") or utcnow_iso()),
            updated_at=str(data.get("updated_at") or utcnow_iso()),
        )

    def to_row(self) -> Dict[str, Optional[str]]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "company": self.company or None,
            "role": self.role or None,
            "loop_interest": self.loop_interest or None,
            "batch_id": self.batch_id,
            "questions": self.questions or None,
            "source": self.source,
            "status": self.status,
            "stripe_checkout_session_id": self.stripe_checkout_session_id,
            "stripe_customer_id": self.stripe_customer_id,
            "stripe_payment_intent_id": self.stripe_payment_intent_id,
            "paid_at": self.paid_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
