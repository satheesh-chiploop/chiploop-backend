import re
from typing import Any, Dict

from .models import WebinarRegistration
from .repository import WebinarRegistrationRepository


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WebinarRegistrationError(ValueError):
    pass


class WebinarRegistrationService:
    def __init__(self, repository: WebinarRegistrationRepository):
        self.repository = repository

    def register(self, payload: Dict[str, Any]) -> WebinarRegistration:
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        preferred_session = str(payload.get("preferred_session") or "").strip()

        if len(name) < 2:
            raise WebinarRegistrationError("name_required")
        if not EMAIL_RE.match(email):
            raise WebinarRegistrationError("valid_email_required")
        if preferred_session not in {"9am_pst", "9pm_pst", "either"}:
            raise WebinarRegistrationError("preferred_session_required")

        registration = WebinarRegistration(
            name=name[:160],
            email=email[:254],
            company=str(payload.get("company") or "").strip()[:160],
            role=str(payload.get("role") or "").strip()[:120],
            loop_interest=str(payload.get("loop_interest") or "").strip()[:120],
            preferred_session=preferred_session,
            questions=str(payload.get("questions") or "").strip()[:1000],
            source=str(payload.get("source") or "landing_page").strip()[:80],
        )
        return self.repository.save(registration)
