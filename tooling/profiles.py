import json
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import DEFAULT_ARTIFACT_POLICY, DEFAULT_PROFILE_ID


def _first_existing(candidates: list[str]) -> Optional[str]:
    for item in candidates:
        if not item:
            continue
        found = shutil.which(item) if os.path.basename(item) == item else item
        if found and (os.path.basename(found) == found or os.path.exists(found)):
            return found
    return None


def _default_profile() -> Dict[str, Any]:
    return {
        "profile_id": DEFAULT_PROFILE_ID,
        "runner": "local_saas",
        "artifact_policy": DEFAULT_ARTIFACT_POLICY,
        "runtime": {
            "python": {"executable": _first_existing([
                os.getenv("CHIPLOOP_PYTHON", ""),
                "/root/chiploop-backend/venv/bin/python",
                "python",
                "python3",
            ])},
            "pip": {"executable": _first_existing([
                os.getenv("CHIPLOOP_PIP", ""),
                "/root/chiploop-backend/venv/bin/pip",
                "pip",
                "pip3",
            ])},
            "cocotb_config": {"executable": _first_existing([
                os.getenv("CHIPLOOP_COCOTB_CONFIG", ""),
                "/root/chiploop-backend/venv/bin/cocotb-config",
                "cocotb-config",
            ])},
            "pytest": {"executable": _first_existing([
                os.getenv("CHIPLOOP_PYTEST", ""),
                "/root/chiploop-backend/venv/bin/pytest",
                "pytest",
            ])},
        },
        "tools": {
            "iverilog": {"executable": _first_existing([
                os.getenv("CHIPLOOP_IVERILOG", ""),
                "/usr/bin/iverilog",
                "iverilog",
            ])},
            "vvp": {"executable": _first_existing([
                os.getenv("CHIPLOOP_VVP", ""),
                "/usr/bin/vvp",
                "vvp",
            ])},
            "verilator": {"executable": _first_existing([
                os.getenv("CHIPLOOP_VERILATOR", ""),
                "/usr/local/bin/verilator",
                "/usr/bin/verilator",
                "verilator",
            ])},
            "verilator_coverage": {"executable": _first_existing([
                os.getenv("CHIPLOOP_VERILATOR_COVERAGE", ""),
                "/usr/local/bin/verilator_coverage",
                "/usr/bin/verilator_coverage",
                "verilator_coverage",
            ])},
            "yosys": {"executable": _first_existing([
                os.getenv("CHIPLOOP_YOSYS", ""),
                "/usr/bin/yosys",
                "yosys",
            ])},
            "sby": {"executable": _first_existing([
                os.getenv("CHIPLOOP_SBY", ""),
                "/usr/local/bin/sby",
                "/usr/bin/sby",
                "sby",
            ])},
            "z3": {"executable": _first_existing([
                os.getenv("CHIPLOOP_Z3", ""),
                "/usr/bin/z3",
                "z3",
            ])},
            "boolector": {"executable": _first_existing([
                os.getenv("CHIPLOOP_BOOLECTOR", ""),
                "/usr/bin/boolector",
                "boolector",
            ])},
            "gem5_x86": {"executable": _first_existing([
                os.getenv("GEM5_X86_BIN", ""),
                "/opt/gem5/build/X86/gem5.opt",
            ])},
            "gem5_riscv": {"executable": _first_existing([
                os.getenv("GEM5_RISCV_BIN", ""),
                "/opt/gem5/build/RISCV/gem5.opt",
            ])},
        },
        "env": {
            "PATH": {
                "prepend": [
                    "/root/chiploop-backend/venv/bin",
                    "/usr/local/bin",
                ]
            }
        },
    }


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=8)
def _load_profile_file(path: str) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def get_tool_profile(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state or {}
    profile = state.get("tool_profile")
    if isinstance(profile, dict):
        return _deep_merge(_default_profile(), profile)

    profile_path = (
        state.get("tool_profile_path")
        or os.getenv("CHIPLOOP_TOOL_PROFILE_PATH")
    )
    if isinstance(profile_path, str) and profile_path and os.path.exists(profile_path):
        loaded = _load_profile_file(profile_path)
        return _deep_merge(_default_profile(), loaded)

    return _default_profile()


def resolve_tool(name: str, state: Optional[Dict[str, Any]] = None, *, kind: str = "tools") -> Optional[str]:
    profile = get_tool_profile(state)
    section = profile.get(kind) if isinstance(profile.get(kind), dict) else {}
    entry = section.get(name)
    if isinstance(entry, dict):
        executable = entry.get("executable") or entry.get("path")
    elif isinstance(entry, str):
        executable = entry
    else:
        executable = None
    if executable:
        return executable
    return shutil.which(name)


def profile_summary(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = get_tool_profile(state)
    return {
        "profile_id": profile.get("profile_id") or DEFAULT_PROFILE_ID,
        "runner": profile.get("runner") or "local_saas",
        "artifact_policy": profile.get("artifact_policy") or DEFAULT_ARTIFACT_POLICY,
        "tools": sorted((profile.get("tools") or {}).keys()),
        "runtime": sorted((profile.get("runtime") or {}).keys()),
    }
