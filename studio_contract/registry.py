import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .specs import AgentSpec, CommandSpec, HookSpec, SkillSpec, ToolSpec, WorkflowSpec


REGISTRY_FILES = {
    "agents": "agents.yaml",
    "skills": "skills.yaml",
    "commands": "commands.yaml",
    "hooks": "hooks.yaml",
    "tools": "tools.yaml",
    "workflows": "workflows.yaml",
}

VALID_EXECUTION_MODES = {"native", "legacy_adapter"}


@dataclass
class StudioRegistry:
    agents: Dict[str, AgentSpec] = field(default_factory=dict)
    skills: Dict[str, SkillSpec] = field(default_factory=dict)
    commands: Dict[str, CommandSpec] = field(default_factory=dict)
    hooks: Dict[str, HookSpec] = field(default_factory=dict)
    tools: Dict[str, ToolSpec] = field(default_factory=dict)
    workflows: Dict[str, WorkflowSpec] = field(default_factory=dict)


def _load_json_compatible_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _items(doc: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = doc.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def load_registry(registry_dir: str = "registry") -> StudioRegistry:
    root = Path(registry_dir)
    if not root.exists() and not root.is_absolute():
        root = Path(__file__).resolve().parents[1] / registry_dir
    docs = {
        key: _load_json_compatible_yaml(root / filename)
        for key, filename in REGISTRY_FILES.items()
    }
    return StudioRegistry(
        agents={spec.name: spec for spec in (AgentSpec.from_dict(x) for x in _items(docs["agents"], "agents"))},
        skills={spec.name: spec for spec in (SkillSpec.from_dict(x) for x in _items(docs["skills"], "skills"))},
        commands={spec.name: spec for spec in (CommandSpec.from_dict(x) for x in _items(docs["commands"], "commands"))},
        hooks={spec.name: spec for spec in (HookSpec.from_dict(x) for x in _items(docs["hooks"], "hooks"))},
        tools={spec.name: spec for spec in (ToolSpec.from_dict(x) for x in _items(docs["tools"], "tools"))},
        workflows={spec.name: spec for spec in (WorkflowSpec.from_dict(x) for x in _items(docs["workflows"], "workflows"))},
    )


def _entrypoint_exists(entrypoint: str) -> bool:
    if ":" not in entrypoint:
        return False
    module_name, attr_name = entrypoint.split(":", 1)
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        return False
    try:
        source = Path(spec.origin).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return f"def {attr_name}(" in source or f"{attr_name} =" in source


def validate_registry(registry: StudioRegistry) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    for name, agent in registry.agents.items():
        for field_name in ("name", "loop_type", "domain", "description", "entrypoint", "execution_mode"):
            if not getattr(agent, field_name):
                errors.append(f"agent {name}: missing {field_name}")
        if agent.execution_mode not in VALID_EXECUTION_MODES:
            errors.append(f"agent {name}: invalid execution_mode {agent.execution_mode}")
        if not _entrypoint_exists(agent.entrypoint):
            errors.append(f"agent {name}: entrypoint not found {agent.entrypoint}")
        for skill in agent.required_skills:
            if skill not in registry.skills:
                errors.append(f"agent {name}: missing skill {skill}")
        for tool in agent.required_tools:
            if tool not in registry.tools:
                errors.append(f"agent {name}: missing tool {tool}")
        for hook in agent.hooks:
            if hook not in registry.hooks:
                errors.append(f"agent {name}: missing hook {hook}")

    for name, command in registry.commands.items():
        if command.agent and command.agent not in registry.agents:
            errors.append(f"command {name}: missing agent {command.agent}")

    for name, workflow in registry.workflows.items():
        for agent_name in workflow.agents:
            if agent_name not in registry.agents:
                errors.append(f"workflow {name}: missing agent {agent_name}")
        for hook in workflow.hooks:
            if hook not in registry.hooks:
                errors.append(f"workflow {name}: missing hook {hook}")

    return not errors, errors


def dry_run_validate(registry_dir: str = "registry") -> Dict[str, Any]:
    registry = load_registry(registry_dir)
    ok, errors = validate_registry(registry)
    return {
        "ok": ok,
        "errors": errors,
        "counts": {
            "agents": len(registry.agents),
            "skills": len(registry.skills),
            "commands": len(registry.commands),
            "hooks": len(registry.hooks),
            "tools": len(registry.tools),
            "workflows": len(registry.workflows),
        },
    }
