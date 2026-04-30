from typing import Dict, Optional

from .models import OnboardingState


class OnboardingRepository:
    def get(self, user_id: str) -> Optional[OnboardingState]:
        raise NotImplementedError

    def save(self, state: OnboardingState) -> OnboardingState:
        raise NotImplementedError


class InMemoryOnboardingRepository(OnboardingRepository):
    def __init__(self):
        self.records: Dict[str, OnboardingState] = {}

    def get(self, user_id: str) -> Optional[OnboardingState]:
        return self.records.get(user_id)

    def save(self, state: OnboardingState) -> OnboardingState:
        self.records[state.user_id] = state
        return state


class SupabaseOnboardingRepository(OnboardingRepository):
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def get(self, user_id: str) -> Optional[OnboardingState]:
        response = (
            self.supabase.table("user_onboarding")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if not getattr(response, "data", None):
            return None
        return OnboardingState.from_dict(response.data)

    def save(self, state: OnboardingState) -> OnboardingState:
        row = state.to_row()
        response = (
            self.supabase.table("user_onboarding")
            .upsert(row, on_conflict="user_id")
            .execute()
        )
        data = getattr(response, "data", None)
        if isinstance(data, list) and data:
            return OnboardingState.from_dict(data[0])
        if isinstance(data, dict):
            return OnboardingState.from_dict(data)
        return state
