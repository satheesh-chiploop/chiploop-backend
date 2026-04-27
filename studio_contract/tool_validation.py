import importlib.util
import shutil
from dataclasses import asdict, dataclass
from typing import Dict, List

from .registry import load_registry
from .specs import ToolSpec


PACKAGE_CHECKS = {
    "cocotb": ["cocotb"],
    "openai_or_portkey_llm": ["openai", "portkey_ai"],
    "supabase": ["supabase"],
}


@dataclass
class ToolAvailability:
    name: str
    kind: str
    optional: bool
    available: bool
    check: str
    detail: str


def _package_available(names: List[str]) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in names)


def check_tool_availability(tool: ToolSpec) -> ToolAvailability:
    if tool.name == "python":
        return ToolAvailability(tool.name, tool.kind, tool.optional, True, "runtime", "current Python process")

    if tool.kind == "external_tool":
        found = shutil.which(tool.name)
        return ToolAvailability(
            tool.name,
            tool.kind,
            tool.optional,
            bool(found),
            f"PATH:{tool.name}",
            found or "not found on PATH",
        )

    package_names = PACKAGE_CHECKS.get(tool.name)
    if package_names:
        return ToolAvailability(
            tool.name,
            tool.kind,
            tool.optional,
            _package_available(package_names),
            "python_package:" + "|".join(package_names),
            "installed" if _package_available(package_names) else "not importable",
        )

    return ToolAvailability(
        tool.name,
        tool.kind,
        tool.optional,
        True,
        "metadata_only",
        "no local availability check configured",
    )


def validate_tool_availability(registry_dir: str = "registry") -> Dict:
    registry = load_registry(registry_dir)
    tools = [check_tool_availability(tool) for tool in registry.tools.values()]
    required_missing = [tool.name for tool in tools if not tool.optional and not tool.available]
    optional_missing = [tool.name for tool in tools if tool.optional and not tool.available]

    return {
        "ok": not required_missing,
        "tool_count": len(tools),
        "required_missing": required_missing,
        "optional_missing": optional_missing,
        "tools": [asdict(tool) for tool in tools],
    }
