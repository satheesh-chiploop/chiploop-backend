from dataclasses import dataclass, field
from typing import Any, Dict, List


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@dataclass
class AgentSpec:
    name: str
    loop_type: str
    domain: str
    description: str
    entrypoint: str
    execution_mode: str
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    artifact_paths: List[str] = field(default_factory=list)
    artifact_types: List[str] = field(default_factory=list)
    required_skills: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    hooks: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentSpec":
        return cls(
            name=str(data.get("name") or ""),
            loop_type=str(data.get("loop_type") or ""),
            domain=str(data.get("domain") or ""),
            description=str(data.get("description") or ""),
            entrypoint=str(data.get("entrypoint") or ""),
            execution_mode=str(data.get("execution_mode") or ""),
            inputs=[str(x) for x in _list(data.get("inputs"))],
            outputs=[str(x) for x in _list(data.get("outputs"))],
            artifact_paths=[str(x) for x in _list(data.get("artifact_paths"))],
            artifact_types=[str(x) for x in _list(data.get("artifact_types"))],
            required_skills=[str(x) for x in _list(data.get("required_skills"))],
            required_tools=[str(x) for x in _list(data.get("required_tools"))],
            hooks=[str(x) for x in _list(data.get("hooks"))],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class SkillSpec:
    name: str
    description: str
    domains: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillSpec":
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            domains=[str(x) for x in _list(data.get("domains"))],
            tools=[str(x) for x in _list(data.get("tools"))],
        )


@dataclass
class CommandSpec:
    name: str
    description: str
    agent: str = ""
    command_type: str = "agent"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommandSpec":
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            agent=str(data.get("agent") or ""),
            command_type=str(data.get("command_type") or "agent"),
        )


@dataclass
class HookSpec:
    name: str
    description: str
    lifecycle: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HookSpec":
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            lifecycle=str(data.get("lifecycle") or ""),
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    kind: str = "external"
    optional: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolSpec":
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            kind=str(data.get("kind") or "external"),
            optional=bool(data.get("optional", True)),
        )


@dataclass
class WorkflowSpec:
    name: str
    loop_type: str
    description: str
    agents: List[str] = field(default_factory=list)
    hooks: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowSpec":
        return cls(
            name=str(data.get("name") or ""),
            loop_type=str(data.get("loop_type") or ""),
            description=str(data.get("description") or ""),
            agents=[str(x) for x in _list(data.get("agents"))],
            hooks=[str(x) for x in _list(data.get("hooks"))],
        )
