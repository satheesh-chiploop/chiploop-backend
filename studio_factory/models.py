from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentFactoryRequest:
    name: str
    natural_language_request: str
    domain: Optional[str] = None
    loop_type: Optional[str] = None
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    required_skills: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    allow_extension: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentFactoryRequest":
        return cls(
            name=str(data.get("name") or ""),
            natural_language_request=str(data.get("natural_language_request") or data.get("request") or ""),
            domain=data.get("domain"),
            loop_type=data.get("loop_type"),
            inputs=[str(x) for x in (data.get("inputs") or [])],
            outputs=[str(x) for x in (data.get("outputs") or [])],
            required_skills=[str(x) for x in (data.get("required_skills") or [])],
            required_tools=[str(x) for x in (data.get("required_tools") or [])],
            allow_extension=bool(data.get("allow_extension", False)),
        )


@dataclass
class GeneratedFilePlan:
    path: str
    description: str
    content: str

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["content_preview"] = data.pop("content")[:400]
        return data


@dataclass
class RegistryPatchPlan:
    path: str
    content: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentFactoryPlan:
    decision: str
    proposed_agent_spec: Dict[str, Any]
    proposed_skill_specs: List[Dict[str, Any]] = field(default_factory=list)
    proposed_tool_refs: List[str] = field(default_factory=list)
    proposed_hook_refs: List[str] = field(default_factory=list)
    files_to_generate: List[GeneratedFilePlan] = field(default_factory=list)
    registry_patch: Optional[RegistryPatchPlan] = None
    validation_checklist: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    exact_match: Optional[str] = None
    similar_matches: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["files_to_generate"] = [f.to_dict() for f in self.files_to_generate]
        data["registry_patch"] = self.registry_patch.to_dict() if self.registry_patch else None
        return data


@dataclass
class AgentFactoryResult:
    ok: bool
    dry_run: bool
    plan: AgentFactoryPlan
    written_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "written_files": self.written_files,
            "errors": self.errors,
        }
