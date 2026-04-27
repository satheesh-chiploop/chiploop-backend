import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .validator import _is_safe_relative


def _list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def custom_agent_to_spec(agent: Dict[str, Any]) -> Dict[str, Any]:
    name = str(agent.get("agent_name") or agent.get("name") or "").strip()
    if not name:
        raise ValueError("custom agent is missing agent_name/name")

    loop_type = str(agent.get("loop_type") or agent.get("domain") or "custom")
    domain = str(agent.get("domain") or loop_type)
    description = str(
        agent.get("description")
        or agent.get("summary")
        or agent.get("prompt")
        or "Custom agent exported for Studio registry review."
    )

    return {
        "name": name,
        "loop_type": loop_type,
        "domain": domain,
        "description": description,
        "entrypoint": str(agent.get("entrypoint") or f"AGENT_REGISTRY:{name}"),
        "execution_mode": str(agent.get("execution_mode") or "legacy_adapter"),
        "inputs": _list(agent.get("inputs")) or ["state"],
        "outputs": _list(agent.get("outputs")) or ["state update"],
        "artifact_paths": _list(agent.get("artifact_paths")),
        "artifact_types": _list(agent.get("artifact_types")) or ["custom_agent_artifact"],
        "required_skills": _list(agent.get("required_skills")),
        "required_tools": _list(agent.get("required_tools")) or ["python", "supabase"],
        "hooks": _list(agent.get("hooks"))
        or [
            "pre_run_validate_inputs",
            "post_run_collect_artifacts",
            "post_run_update_state",
            "on_failure_capture_traceback",
            "on_failure_preserve_logs",
            "artifact_publish_to_supabase",
        ],
        "metadata": {
            "exported_by": "custom_agent_export_v1",
            "review_required": True,
            "source": str(agent.get("source") or "custom_agent_metadata"),
        },
    }


def build_custom_agent_registry_patch(custom_agents: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    specs = [custom_agent_to_spec(agent) for agent in custom_agents]
    return {
        "agents": specs,
        "review_required": True,
        "instructions": "Review these dynamic custom-agent specs before manually merging into registry/agents.yaml.",
    }


def load_custom_agent_export(path: str) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("agents") or data.get("custom_agents") or []
    if not isinstance(data, list):
        raise ValueError("custom agent export input must be a list or contain agents/custom_agents")
    return [dict(item) for item in data]


def write_custom_agent_patch(patch: Dict[str, Any], output: str) -> str:
    if not _is_safe_relative(output):
        raise ValueError(f"Unsafe custom-agent export output path: {output}")
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(patch, indent=2) + "\n", encoding="utf-8")
    return str(target)
