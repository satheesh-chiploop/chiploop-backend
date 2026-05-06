from typing import Dict, Optional

from .models import WorkshopRegistration


class WorkshopRegistrationRepository:
    def count_paid_by_batch(self, batch_id: str) -> int:
        raise NotImplementedError

    def get(self, registration_id: str) -> Optional[WorkshopRegistration]:
        raise NotImplementedError

    def get_by_checkout_session(self, checkout_session_id: str) -> Optional[WorkshopRegistration]:
        raise NotImplementedError

    def save(self, registration: WorkshopRegistration) -> WorkshopRegistration:
        raise NotImplementedError


class InMemoryWorkshopRegistrationRepository(WorkshopRegistrationRepository):
    def __init__(self):
        self.records: Dict[str, WorkshopRegistration] = {}

    def count_paid_by_batch(self, batch_id: str) -> int:
        return sum(
            1
            for registration in self.records.values()
            if registration.batch_id == batch_id and registration.status == "paid"
        )

    def get(self, registration_id: str) -> Optional[WorkshopRegistration]:
        return self.records.get(registration_id)

    def get_by_checkout_session(self, checkout_session_id: str) -> Optional[WorkshopRegistration]:
        for registration in self.records.values():
            if registration.stripe_checkout_session_id == checkout_session_id:
                return registration
        return None

    def save(self, registration: WorkshopRegistration) -> WorkshopRegistration:
        self.records[registration.id] = registration
        return registration


class SupabaseWorkshopRegistrationRepository(WorkshopRegistrationRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def count_paid_by_batch(self, batch_id: str) -> int:
        response = (
            self.supabase.table("workshop_registrations")
            .select("id", count="exact")
            .eq("batch_id", batch_id)
            .eq("status", "paid")
            .execute()
        )
        return int(getattr(response, "count", 0) or 0)

    def get(self, registration_id: str) -> Optional[WorkshopRegistration]:
        response = (
            self.supabase.table("workshop_registrations")
            .select("*")
            .eq("id", registration_id)
            .maybe_single()
            .execute()
        )
        if not getattr(response, "data", None):
            return None
        return WorkshopRegistration.from_dict(response.data)

    def get_by_checkout_session(self, checkout_session_id: str) -> Optional[WorkshopRegistration]:
        response = (
            self.supabase.table("workshop_registrations")
            .select("*")
            .eq("stripe_checkout_session_id", checkout_session_id)
            .maybe_single()
            .execute()
        )
        if not getattr(response, "data", None):
            return None
        return WorkshopRegistration.from_dict(response.data)

    def save(self, registration: WorkshopRegistration) -> WorkshopRegistration:
        response = (
            self.supabase.table("workshop_registrations")
            .upsert(registration.to_row(), on_conflict="id")
            .execute()
        )
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            return WorkshopRegistration.from_dict(data[0])
        if isinstance(data, dict):
            return WorkshopRegistration.from_dict(data)
        return registration
