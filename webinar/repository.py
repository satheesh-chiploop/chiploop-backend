from typing import Dict

from .models import WebinarRegistration


class WebinarRegistrationRepository:
    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        raise NotImplementedError


class InMemoryWebinarRegistrationRepository(WebinarRegistrationRepository):
    def __init__(self):
        self.records: Dict[str, WebinarRegistration] = {}

    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        self.records[registration.id] = registration
        return registration


class SupabaseWebinarRegistrationRepository(WebinarRegistrationRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def save(self, registration: WebinarRegistration) -> WebinarRegistration:
        response = self.supabase.table("webinar_registrations").insert(registration.to_row()).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            return WebinarRegistration.from_dict(data[0])
        if isinstance(data, dict):
            return WebinarRegistration.from_dict(data)
        return registration
