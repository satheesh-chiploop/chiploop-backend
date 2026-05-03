from typing import Dict

from .models import WebinarRegistration


class WebinarRegistrationRepository:
    def count_by_session(self, preferred_session: str) -> int:
        raise NotImplementedError

    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        raise NotImplementedError


class InMemoryWebinarRegistrationRepository(WebinarRegistrationRepository):
    def __init__(self):
        self.records: Dict[str, WebinarRegistration] = {}

    def count_by_session(self, preferred_session: str) -> int:
        return sum(
            1
            for registration in self.records.values()
            if registration.preferred_session == preferred_session and registration.status == "registered"
        )

    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        self.records[registration.id] = registration
        return registration


class SupabaseWebinarRegistrationRepository(WebinarRegistrationRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def count_by_session(self, preferred_session: str) -> int:
        response = (
            self.supabase.table("webinar_registrations")
            .select("id", count="exact")
            .eq("preferred_session", preferred_session)
            .eq("status", "registered")
            .execute()
        )
        return int(getattr(response, "count", 0) or 0)

    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        response = self.supabase.table("webinar_registrations").insert(registration.to_row()).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            return WebinarRegistration.from_dict(data[0])
        if isinstance(data, dict):
            return WebinarRegistration.from_dict(data)
        return registration
