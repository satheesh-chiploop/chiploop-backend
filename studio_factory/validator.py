import json
from pathlib import Path, PurePosixPath
from typing import List

from studio_contract.registry import load_registry

from .models import AgentFactoryPlan


ALLOWED_OUTPUT_ROOTS = {
    "generated_studio_agents",
    "generated_patches",
    "examples/studio_factory",
}
BLOCKED_OUTPUT_ROOTS = {"agents", "registry", "main.py", "studio_contract", "studio_planner"}


def _is_safe_relative(path: str) -> bool:
    if not path:
        return False
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return False
    as_string = str(p)
    if p.parts[0] in BLOCKED_OUTPUT_ROOTS or as_string in BLOCKED_OUTPUT_ROOTS:
        return False
    return any(as_string == root or as_string.startswith(f"{root}/") for root in ALLOWED_OUTPUT_ROOTS)


def validate_factory_plan(plan: AgentFactoryPlan, registry_dir: str = "registry") -> List[str]:
    errors: List[str] = []
    registry = load_registry(registry_dir)

    if plan.decision in {"reuse_existing", "extend_or_create_variant"}:
        return errors

    spec = plan.proposed_agent_spec
    for field in ("name", "loop_type", "domain", "description", "entrypoint", "execution_mode"):
        if not spec.get(field):
            errors.append(f"proposed_agent_spec missing {field}")

    for tool in plan.proposed_tool_refs:
        if tool not in registry.tools:
            errors.append(f"missing tool reference {tool}")

    for hook in plan.proposed_hook_refs:
        if hook not in registry.hooks:
            errors.append(f"missing hook reference {hook}")

    for file_plan in plan.files_to_generate:
        if not _is_safe_relative(file_plan.path):
            errors.append(f"unsafe generated file path {file_plan.path}")

    if plan.registry_patch:
        if not _is_safe_relative(plan.registry_patch.path):
            errors.append(f"unsafe registry patch path {plan.registry_patch.path}")
        try:
            json.loads(plan.registry_patch.content)
        except Exception as exc:
            errors.append(f"registry patch is not valid JSON-compatible YAML: {exc}")

    try:
        json.dumps(plan.to_dict())
    except Exception as exc:
        errors.append(f"plan is not JSON serializable: {exc}")

    return errors


def assert_output_base_safe(output_dir: str) -> Path:
    base = Path(output_dir)
    as_posix = base.as_posix().strip("/")
    if base.is_absolute() or ".." in base.parts:
        raise ValueError(f"Unsafe output directory: {output_dir}")
    if as_posix in {"", "."}:
        return base
    if as_posix and not any(as_posix == root or as_posix.startswith(f"{root}/") for root in ALLOWED_OUTPUT_ROOTS):
        raise ValueError(f"Output directory must be under one of {sorted(ALLOWED_OUTPUT_ROOTS)}")
    return base
