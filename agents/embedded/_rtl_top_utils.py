import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


RTL_SUFFIXES = (".sv", ".v", ".svh", ".vh")


def strip_sv_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text or "", flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def parse_module_names(text: str) -> List[str]:
    names: List[str] = []
    for match in re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", strip_sv_comments(text)):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def _resolve_path(path: str, workflow_dir: str) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    if workflow_dir:
        joined = Path(workflow_dir) / path
        if joined.exists():
            return str(joined)
    return str(candidate)


def _iter_filelist_entries(path: str, workflow_dir: str) -> Iterable[str]:
    text = _safe_read(path)
    if not text:
        return []

    base_dir = str(Path(path).resolve().parent)
    entries: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//", "+incdir+")):
            continue
        if line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()
        if not line.lower().endswith(RTL_SUFFIXES):
            continue
        resolved = _resolve_path(line, workflow_dir)
        if not os.path.isfile(resolved):
            resolved = _resolve_path(line, base_dir)
        entries.append(resolved)
    return entries


def collect_rtl_paths(state: Dict[str, Any], workflow_dir: str = "") -> List[str]:
    paths: List[str] = []
    containers = [state, state.get("system") or {}, state.get("digital") or {}]

    list_keys = [
        "rtl_inputs",
        "system_rtl_files",
        "system_rtl_filelist_sim",
        "rtl_files",
        "verilog_sources",
    ]
    path_keys = [
        "local_rtl_path",
        "soc_top_sim_path",
        "system_top_sim_path",
        "rtl_top_path",
    ]
    filelist_keys = [
        "rtl_filelist_path",
        "system_filelist_sim_path",
        "filelist_path",
    ]

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in list_keys:
            value = container.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        paths.append(_resolve_path(item.strip(), workflow_dir))
        for key in path_keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(_resolve_path(value.strip(), workflow_dir))
        for key in filelist_keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                filelist = _resolve_path(value.strip(), workflow_dir)
                paths.extend(_iter_filelist_entries(filelist, workflow_dir))

    out: List[str] = []
    for path in paths:
        normalized = str(Path(path)) if path else ""
        if not normalized.lower().endswith(RTL_SUFFIXES):
            continue
        if not os.path.isfile(normalized):
            continue
        if normalized not in out:
            out.append(normalized)
    return out


def resolve_rtl_top_from_files(candidate_top: str, rtl_paths: List[str]) -> Tuple[str, Dict[str, Any]]:
    candidate_top = (candidate_top or "").strip()
    module_map: Dict[str, List[str]] = {}
    ordered_modules: List[str] = []

    for path in rtl_paths:
        modules = parse_module_names(_safe_read(path))
        module_map[path] = modules
        for module in modules:
            if module not in ordered_modules:
                ordered_modules.append(module)

    if candidate_top and any(candidate_top in mods for mods in module_map.values()):
        return candidate_top, {
            "resolution": "metadata_top_found_in_rtl",
            "candidate_top": candidate_top,
            "module_map": module_map,
        }

    selected = candidate_top
    reason = "metadata_top_used_without_rtl_match"
    if len(ordered_modules) == 1:
        selected = ordered_modules[0]
        reason = "single_rtl_module_selected"
    elif len(rtl_paths) == 1 and module_map.get(rtl_paths[0]):
        selected = module_map[rtl_paths[0]][0]
        reason = "first_module_in_single_rtl_file_selected"

    return selected, {
        "resolution": reason,
        "candidate_top": candidate_top,
        "selected_top": selected,
        "module_map": module_map,
    }


def apply_resolved_top(state: Dict[str, Any], top_module: str) -> None:
    if not top_module:
        return
    state["top_module"] = top_module
    state["rtl_top"] = top_module
    state["soc_top_sim_module"] = top_module
    state["system_top_sim_module"] = top_module
    system = state.setdefault("system", {})
    if isinstance(system, dict):
        system["soc_top_sim_module"] = top_module
        system["system_top_sim_module"] = top_module
