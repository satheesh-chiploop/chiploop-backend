from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentPlanRequest:
    natural_language_request: str
    domain: Optional[str] = None
    loop_type: Optional[str] = None
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentPlanRequest":
        return cls(
            natural_language_request=str(data.get("natural_language_request") or data.get("request") or ""),
            domain=data.get("domain"),
            loop_type=data.get("loop_type"),
            inputs=[str(x) for x in (data.get("inputs") or [])],
            outputs=[str(x) for x in (data.get("outputs") or [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExistingAgentMatch:
    name: str
    score: float
    match_reasons: List[str] = field(default_factory=list)
    loop_type: str = ""
    domain: str = ""
    entrypoint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentPlanResult:
    requested_capability: str
    exact_matches: List[ExistingAgentMatch] = field(default_factory=list)
    similar_matches: List[ExistingAgentMatch] = field(default_factory=list)
    reusable_skills: List[str] = field(default_factory=list)
    reusable_tools: List[str] = field(default_factory=list)
    reusable_hooks: List[str] = field(default_factory=list)
    missing_capabilities: List[str] = field(default_factory=list)
    recommendation: str = "create_new"
    confidence_score: float = 0.0
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["exact_matches"] = [m.to_dict() for m in self.exact_matches]
        data["similar_matches"] = [m.to_dict() for m in self.similar_matches]
        return data
