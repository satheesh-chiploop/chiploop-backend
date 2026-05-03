from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


ARCH2RTL_DEMO_LIMIT = 3


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OnboardingState:
    user_id: str
    completed_at: Optional[str] = None
    skipped_at: Optional[str] = None
    demo_started_at: Optional[str] = None
    first_workflow_id: Optional[str] = None
    last_step: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    @property
    def completed(self) -> bool:
        return bool(self.completed_at or self.skipped_at)

    @property
    def arch2rtl_demo_runs(self) -> int:
        try:
            return int(self.metadata.get("arch2rtl_demo_runs") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def arch2rtl_demo_runs_remaining(self) -> int:
        return max(ARCH2RTL_DEMO_LIMIT - self.arch2rtl_demo_runs, 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "completed": self.completed,
            "completed_at": self.completed_at,
            "skipped_at": self.skipped_at,
            "demo_started_at": self.demo_started_at,
            "first_workflow_id": self.first_workflow_id,
            "last_step": self.last_step,
            "metadata": self.metadata,
            "demo": {
                "arch2rtl": {
                    "limit": ARCH2RTL_DEMO_LIMIT,
                    "runs_used": self.arch2rtl_demo_runs,
                    "runs_remaining": self.arch2rtl_demo_runs_remaining,
                    "can_run": self.arch2rtl_demo_runs_remaining > 0,
                }
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_row(self) -> Dict[str, Any]:
        data = self.to_dict()
        data.pop("completed", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OnboardingState":
        return cls(
            user_id=str(data.get("user_id") or ""),
            completed_at=data.get("completed_at"),
            skipped_at=data.get("skipped_at"),
            demo_started_at=data.get("demo_started_at"),
            first_workflow_id=data.get("first_workflow_id"),
            last_step=data.get("last_step"),
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or utcnow_iso()),
            updated_at=str(data.get("updated_at") or utcnow_iso()),
        )
