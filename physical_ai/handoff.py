"""Contracts shared by the Physical AI workflow and its downstream loops."""

import re
from typing import Any, Dict, Tuple


def resolve_design_identity(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Resolve the governed RTL top and project names for a Physical AI run."""
    digital_ip = payload.get("digital_ip_spec") if isinstance(payload.get("digital_ip_spec"), dict) else {}
    architecture = payload.get("generated_architecture") if isinstance(payload.get("generated_architecture"), dict) else {}
    top_module = str(
        payload.get("top_module")
        or payload.get("model_top_module")
        or digital_ip.get("top_module")
        or architecture.get("top_module")
        or ""
    ).strip()
    project_name = str(
        payload.get("project_name")
        or digital_ip.get("project_name")
        or payload.get("model_project_name")
        or digital_ip.get("name")
        or architecture.get("product_name")
        or payload.get("application")
        or "physical_ai_product"
    ).strip()
    if not top_module:
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", project_name).strip("_").lower() or "physical_ai"
        stem = re.sub(r"_ip$", "", stem)
        top_module = f"{stem}_top"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top_module):
        raise ValueError(f"Invalid Physical AI top module identity: {top_module}")
    project_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("_") or "physical_ai_product"
    return top_module, project_name
