import re
from typing import Any, Dict, List

from .models import WebinarRegistration
from .repository import WebinarRegistrationRepository


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WEBINAR_SESSION_CAPACITY = 10
WEBINAR_SESSIONS = [
    {
        "id": "2026-05-23_09am_pt",
        "label": "May 23, 2026, 9:00 AM PST",
        "date": "2026-05-23",
        "time": "9:00 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-05-23_09pm_pt",
        "label": "May 23, 2026, 9:00 PM PST",
        "date": "2026-05-23",
        "time": "9:00 PM",
        "timezone": "PST",
    },
    {
        "id": "2026-05-30_09am_pt",
        "label": "May 30, 2026, 9:00 AM PST",
        "date": "2026-05-30",
        "time": "9:00 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-05-30_09pm_pt",
        "label": "May 30, 2026, 9:00 PM PST",
        "date": "2026-05-30",
        "time": "9:00 PM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-06_09am_pt",
        "label": "June 6, 2026, 9:00 AM PST",
        "date": "2026-06-06",
        "time": "9:00 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-06_09pm_pt",
        "label": "June 6, 2026, 9:00 PM PST",
        "date": "2026-06-06",
        "time": "9:00 PM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-13_09am_pt",
        "label": "June 13, 2026, 9:00 AM PST",
        "date": "2026-06-13",
        "time": "9:00 AM",
        "timezone": "PST",
    },
    {
        "id": "2026-06-13_09pm_pt",
        "label": "June 13, 2026, 9:00 PM PST",
        "date": "2026-06-13",
        "time": "9:00 PM",
        "timezone": "PST",
    },
]
WEBINAR_SESSION_IDS = {session["id"] for session in WEBINAR_SESSIONS}


class WebinarRegistrationError(ValueError):
    pass


class WebinarRegistrationService:
    def __init__(self, repository: WebinarRegistrationRepository):
        self.repository = repository

    def sessions(self) -> List[Dict[str, Any]]:
        sessions = []
        for session in WEBINAR_SESSIONS:
            booked = self.repository.count_by_session(str(session["id"]))
            sessions.append({
                **session,
                "capacity": WEBINAR_SESSION_CAPACITY,
                "booked": booked,
                "remaining": max(WEBINAR_SESSION_CAPACITY - booked, 0),
                "full": booked >= WEBINAR_SESSION_CAPACITY,
            })
        return sessions

    def register(self, payload: Dict[str, Any]) -> WebinarRegistration:
        name = str(payload.get("name") or "").strip()
        email = str(payload.get("email") or "").strip().lower()
        preferred_session = str(payload.get("preferred_session") or "").strip()

        if len(name) < 2:
            raise WebinarRegistrationError("name_required")
        if not EMAIL_RE.match(email):
            raise WebinarRegistrationError("valid_email_required")
        if preferred_session not in WEBINAR_SESSION_IDS:
            raise WebinarRegistrationError("preferred_session_required")
        if self.repository.count_by_session(preferred_session) >= WEBINAR_SESSION_CAPACITY:
            raise WebinarRegistrationError("preferred_session_full")

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
