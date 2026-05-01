from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WorkflowRun:
    workflow_id: str
    run_id: Optional[str] = None
    loop_type: Optional[str] = None
    status: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowRun":
        return cls(
            workflow_id=str(data.get("workflow_id") or data.get("id") or ""),
            run_id=data.get("run_id"),
            loop_type=data.get("loop_type"),
            status=data.get("status"),
            raw=dict(data),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRef:
    name: str
    workflow_id: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowStatus:
    workflow_id: str
    status: Optional[str] = None
    phase: Optional[str] = None
    loop_type: Optional[str] = None
    logs: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowStatus":
        return cls(
            workflow_id=str(data.get("workflow_id") or data.get("id") or ""),
            status=data.get("status"),
            phase=data.get("phase"),
            loop_type=data.get("loop_type"),
            logs=data.get("logs"),
            raw=dict(data),
        )


@dataclass
class PlanSummary:
    plan_name: Optional[str] = None
    credits: Optional[int] = None
    credits_used: Optional[int] = None
    credits_remaining: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanSummary":
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else data
        return cls(
            plan_name=plan.get("plan_name") or (plan.get("current_plan") or {}).get("display_name"),
            credits=plan.get("credits") or plan.get("monthly_credits"),
            credits_used=plan.get("credits_used"),
            credits_remaining=plan.get("credits_remaining"),
            raw=dict(data),
        )


@dataclass
class UsageSummary:
    recent_event_count: int = 0
    by_event_type: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageSummary":
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
        return cls(
            recent_event_count=int(usage.get("recent_event_count") or 0),
            by_event_type=dict(usage.get("by_event_type") or {}),
            raw=dict(data),
        )
