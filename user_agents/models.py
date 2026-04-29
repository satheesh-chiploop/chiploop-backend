from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass
class PrivateAgentPayload:
    name: str
    loop_type: str = "system"
    description: str = ""
    agent_spec: Dict[str, Any] = field(default_factory=dict)
    skills: List[Any] = field(default_factory=list)
    tools: List[Any] = field(default_factory=list)
    hooks: List[Any] = field(default_factory=list)
    generated_files: List[Any] = field(default_factory=list)
    registry_patch: Dict[str, Any] = field(default_factory=dict)
    source: str = "studio_factory"
    status: str = "private"
    visibility: str = "private"
    script_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrivateAgentPayload":
        raw_spec = _dict(data.get("agent_spec") or data.get("proposed_agent_spec"))
        plan = _dict(data.get("plan"))
        if not raw_spec and isinstance(plan.get("proposed_agent_spec"), dict):
            raw_spec = plan["proposed_agent_spec"]

        name = (
            data.get("agent_name")
            or data.get("name")
            or raw_spec.get("name")
            or ""
        )
        loop_type = data.get("loop_type") or raw_spec.get("loop_type") or raw_spec.get("domain") or "system"
        description = data.get("description") or raw_spec.get("description") or ""

        return cls(
            name=str(name).strip(),
            loop_type=str(loop_type or "system").strip().lower(),
            description=str(description or ""),
            agent_spec=raw_spec,
            skills=_list(data.get("skills") or data.get("required_skills") or raw_spec.get("required_skills")),
            tools=_list(data.get("tools") or data.get("required_tools") or raw_spec.get("required_tools")),
            hooks=_list(data.get("hooks") or raw_spec.get("hooks")),
            generated_files=_list(data.get("generated_files") or data.get("files_to_generate") or plan.get("files_to_generate")),
            registry_patch=_dict(data.get("registry_patch") or plan.get("registry_patch")),
            source=str(data.get("source") or "studio_factory"),
            status=str(data.get("status") or "private"),
            visibility=str(data.get("visibility") or "private"),
            script_path=data.get("script_path"),
        )

    def to_agent_row(self, user_id: str) -> Dict[str, Any]:
        if not self.name:
            raise ValueError("agent name is required")
        visibility = "private" if self.visibility not in {"private", "global", "marketplace", "marketplace_pending"} else self.visibility
        status = "private" if self.status not in {"draft", "private", "submitted", "approved", "rejected"} else self.status
        return {
            "owner_id": user_id,
            "agent_name": self.name,
            "loop_type": self.loop_type or "system",
            "description": self.description,
            "script_path": self.script_path,
            "is_custom": True,
            "is_prebuilt": False,
            "is_marketplace": False,
            "agent_spec": self.agent_spec,
            "skills": self.skills,
            "tools": self.tools,
            "hooks": self.hooks,
            "generated_files": self.generated_files,
            "registry_patch": self.registry_patch,
            "status": status,
            "visibility": visibility,
            "source": self.source or "studio_factory",
            "updated_at": utc_now_iso(),
        }


@dataclass
class PrivateAgent:
    id: str
    owner_id: str
    agent_name: str
    loop_type: str = "system"
    description: str = ""
    script_path: Optional[str] = None
    is_custom: bool = True
    is_prebuilt: bool = False
    is_marketplace: bool = False
    agent_spec: Dict[str, Any] = field(default_factory=dict)
    skills: List[Any] = field(default_factory=list)
    tools: List[Any] = field(default_factory=list)
    hooks: List[Any] = field(default_factory=list)
    generated_files: List[Any] = field(default_factory=list)
    registry_patch: Dict[str, Any] = field(default_factory=dict)
    status: str = "private"
    visibility: str = "private"
    source: str = "studio_factory"
    submitted_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    review_notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "PrivateAgent":
        return cls(
            id=str(row.get("id") or ""),
            owner_id=str(row.get("owner_id") or ""),
            agent_name=str(row.get("agent_name") or row.get("name") or ""),
            loop_type=str(row.get("loop_type") or "system"),
            description=str(row.get("description") or ""),
            script_path=row.get("script_path"),
            is_custom=bool(row.get("is_custom", True)),
            is_prebuilt=bool(row.get("is_prebuilt", False)),
            is_marketplace=bool(row.get("is_marketplace", False)),
            agent_spec=_dict(row.get("agent_spec")),
            skills=_list(row.get("skills")),
            tools=_list(row.get("tools")),
            hooks=_list(row.get("hooks")),
            generated_files=_list(row.get("generated_files")),
            registry_patch=_dict(row.get("registry_patch")),
            status=str(row.get("status") or "private"),
            visibility=str(row.get("visibility") or "private"),
            source=str(row.get("source") or "studio_factory"),
            submitted_at=row.get("submitted_at"),
            reviewed_at=row.get("reviewed_at"),
            reviewed_by=row.get("reviewed_by"),
            review_notes=row.get("review_notes"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
