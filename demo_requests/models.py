from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DemoRequest:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    email: str = ""
    phone: str = ""
    organization_type: str = ""
    organization_name: str = ""
    role: str = ""
    interest_area: str = ""
    preferred_time: str = ""
    message: str = ""
    source: str = "landing_page"
    status: str = "new"
    notification_to: str = "chiploop.agx@gmail.com"
    notification_status: str = "pending"
    notification_error: str = ""
    notification_sent_at: str = ""
    created_at: str = field(default_factory=utcnow_iso)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DemoRequest":
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or ""),
            email=str(data.get("email") or ""),
            phone=str(data.get("phone") or ""),
            organization_type=str(data.get("organization_type") or ""),
            organization_name=str(data.get("organization_name") or ""),
            role=str(data.get("role") or ""),
            interest_area=str(data.get("interest_area") or ""),
            preferred_time=str(data.get("preferred_time") or ""),
            message=str(data.get("message") or ""),
            source=str(data.get("source") or "landing_page"),
            status=str(data.get("status") or "new"),
            notification_to=str(data.get("notification_to") or "chiploop.agx@gmail.com"),
            notification_status=str(data.get("notification_status") or "pending"),
            notification_error=str(data.get("notification_error") or ""),
            notification_sent_at=str(data.get("notification_sent_at") or ""),
            created_at=str(data.get("created_at") or utcnow_iso()),
        )

    def to_row(self) -> Dict[str, Optional[str]]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone or None,
            "organization_type": self.organization_type,
            "organization_name": self.organization_name or None,
            "role": self.role or None,
            "interest_area": self.interest_area or None,
            "preferred_time": self.preferred_time or None,
            "message": self.message or None,
            "source": self.source,
            "status": self.status,
            "notification_to": self.notification_to,
            "notification_status": self.notification_status,
            "notification_error": self.notification_error or None,
            "notification_sent_at": self.notification_sent_at or None,
            "created_at": self.created_at,
        }
