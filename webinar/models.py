from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WebinarRegistration:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    email: str = ""
    company: str = ""
    role: str = ""
    loop_interest: str = ""
    preferred_session: str = ""
    questions: str = ""
    source: str = "landing_page"
    status: str = "registered"
    created_at: str = field(default_factory=utcnow_iso)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebinarRegistration":
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or ""),
            email=str(data.get("email") or ""),
            company=str(data.get("company") or ""),
            role=str(data.get("role") or ""),
            loop_interest=str(data.get("loop_interest") or ""),
            preferred_session=str(data.get("preferred_session") or ""),
            questions=str(data.get("questions") or ""),
            source=str(data.get("source") or "landing_page"),
            status=str(data.get("status") or "registered"),
            created_at=str(data.get("created_at") or utcnow_iso()),
        )

    def to_row(self) -> Dict[str, Optional[str]]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "company": self.company or None,
            "role": self.role or None,
            "loop_interest": self.loop_interest or None,
            "preferred_session": self.preferred_session or None,
            "questions": self.questions or None,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
        }
