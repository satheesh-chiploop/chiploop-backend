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
