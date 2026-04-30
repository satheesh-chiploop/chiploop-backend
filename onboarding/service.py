from typing import Any, Dict

from .models import OnboardingState, utcnow_iso
from .repository import OnboardingRepository


class OnboardingService:
    def __init__(self, repository: OnboardingRepository):
        self.repository = repository

    def get_state(self, user_id: str) -> OnboardingState:
        return self.repository.get(user_id) or OnboardingState(user_id=user_id)

    def update_state(self, user_id: str, payload: Dict[str, Any]) -> OnboardingState:
        state = self.get_state(user_id)
        action = str(payload.get("action") or "").strip().lower()
        now = utcnow_iso()

        if action == "start":
            state.demo_started_at = state.demo_started_at or now
            state.last_step = str(payload.get("last_step") or "started")
        elif action == "complete":
            state.completed_at = state.completed_at or now
            state.last_step = str(payload.get("last_step") or "downloaded")
            if payload.get("workflow_id"):
                state.first_workflow_id = str(payload["workflow_id"])
        elif action == "skip":
            state.skipped_at = state.skipped_at or now
            state.last_step = str(payload.get("last_step") or "skipped")
        elif payload.get("last_step"):
            state.last_step = str(payload["last_step"])

        if payload.get("metadata") and isinstance(payload["metadata"], dict):
            state.metadata = {**state.metadata, **payload["metadata"]}

        state.updated_at = now
        return self.repository.save(state)
