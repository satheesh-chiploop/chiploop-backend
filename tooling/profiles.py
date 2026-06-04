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
        "strict_tool_profile": False,
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
            "python3": {"executable": _first_existing([
                os.getenv("CHIPLOOP_PYTHON3", ""),
                "/root/chiploop-backend/venv/bin/python3",
                "/usr/bin/python3",
                "python3",
                "python",
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
            "make": {"executable": _first_existing([
                os.getenv("CHIPLOOP_MAKE", ""),
                "/usr/bin/make",
                "make",
            ])},
            "cargo": {"executable": _first_existing([
                os.getenv("CHIPLOOP_CARGO", ""),
                "/root/.cargo/bin/cargo",
                "/usr/bin/cargo",
                "cargo",
            ])},
            "rustc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_RUSTC", ""),
                "/root/.cargo/bin/rustc",
                "/usr/bin/rustc",
                "rustc",
            ])},
            "llvm_cov": {"executable": _first_existing([
                os.getenv("CHIPLOOP_LLVM_COV", ""),
                "llvm-cov",
            ])},
            "llvm_profdata": {"executable": _first_existing([
                os.getenv("CHIPLOOP_LLVM_PROFDATA", ""),
                "llvm-profdata",
            ])},
            "gcov": {"executable": _first_existing([
                os.getenv("CHIPLOOP_GCOV", ""),
                "gcov",
            ])},
            "git": {"executable": _first_existing([
                os.getenv("CHIPLOOP_GIT", ""),
                "/usr/bin/git",
                "git",
            ])},
            "bash": {"executable": _first_existing([
                os.getenv("CHIPLOOP_BASH", ""),
                "/bin/bash",
                "bash",
            ])},
            "scons": {"executable": _first_existing([
                os.getenv("CHIPLOOP_SCONS", ""),
                "/usr/bin/scons",
                "scons",
            ])},
            "gcc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_GCC", ""),
                "/usr/bin/gcc",
                "gcc",
            ])},
            "ngspice": {"executable": _first_existing([
                os.getenv("CHIPLOOP_NGSPICE", ""),
                "/usr/bin/ngspice",
                "ngspice",
            ])},
            "docker": {"executable": _first_existing([
                os.getenv("CHIPLOOP_DOCKER", ""),
                "/usr/bin/docker",
                "docker",
            ])},
            "openroad": {"executable": _first_existing([
                os.getenv("CHIPLOOP_OPENROAD", ""),
                "/usr/bin/openroad",
                "/usr/local/bin/openroad",
                "openroad",
            ])},
            "fault": {"executable": _first_existing([
                os.getenv("CHIPLOOP_FAULT", ""),
                "/usr/local/bin/fault",
                "/usr/bin/fault",
                "fault",
            ])},
            "atalanta": {"executable": _first_existing([
                os.getenv("CHIPLOOP_ATALANTA", ""),
                "/usr/local/bin/atalanta",
                "/usr/bin/atalanta",
                "atalanta",
            ])},
            "podem": {"executable": _first_existing([
                os.getenv("CHIPLOOP_PODEM", ""),
                "/usr/local/bin/podem",
                "/usr/bin/podem",
                "podem",
            ])},
            "openram": {"executable": _first_existing([
                os.getenv("CHIPLOOP_OPENRAM", ""),
                "/usr/local/bin/openram",
                "/usr/bin/openram",
                "openram",
            ])},
            "autombist": {"executable": _first_existing([
                os.getenv("CHIPLOOP_AUTOMBIST", ""),
                "/usr/local/bin/autombist",
                "/usr/bin/autombist",
                "autombist",
            ])},
            "synopsys_dc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_SYNOPSYS_DC", ""),
                os.getenv("DC_SHELL", ""),
                "dc_shell",
            ])},
            "cadence_genus": {"executable": _first_existing([
                os.getenv("CHIPLOOP_CADENCE_GENUS", ""),
                os.getenv("GENUS_BIN", ""),
                "genus",
            ])},
            "xcelium": {"executable": _first_existing([
                os.getenv("CHIPLOOP_XCELIUM", ""),
                os.getenv("XRUN", ""),
                "xrun",
            ])},
            "vcs": {"executable": _first_existing([
                os.getenv("CHIPLOOP_VCS", ""),
                "vcs",
            ])},
            "questa": {"executable": _first_existing([
                os.getenv("CHIPLOOP_QUESTA", ""),
                os.getenv("VSIM", ""),
                "vsim",
            ])},
            "vc_lp": {"executable": _first_existing([
                os.getenv("CHIPLOOP_VC_LP", ""),
                "vc_static_shell",
                "vcst",
            ])},
            "jasper": {"executable": _first_existing([
                os.getenv("CHIPLOOP_JASPER", ""),
                "jaspergold",
            ])},
            "riscv64_linux_gnu_gcc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_RISCV64_LINUX_GNU_GCC", ""),
                "riscv64-linux-gnu-gcc",
            ])},
            "riscv64_unknown_elf_gcc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_RISCV64_UNKNOWN_ELF_GCC", ""),
                "riscv64-unknown-elf-gcc",
            ])},
            "x86_64_linux_gnu_gcc": {"executable": _first_existing([
                os.getenv("CHIPLOOP_X86_64_LINUX_GNU_GCC", ""),
                "x86_64-linux-gnu-gcc",
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
        "commands": {
            "upf_static_check": {
                "command": os.getenv("CHIPLOOP_UPF_STATIC_CHECK_COMMAND", ""),
                "description": "Optional private low-power static check command. CHIPLOOP_UPF_FILE and CHIPLOOP_UPF_STAGE_DIR are exported when invoked.",
            },
            "upf_power_aware_sim": {
                "command": os.getenv("CHIPLOOP_UPF_POWER_AWARE_SIM_COMMAND", ""),
                "description": "Optional private power-aware simulation command for licensed simulators.",
            },
        },
    }


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in {"tools", "runtime", "commands"} and not isinstance(value, dict):
            continue
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
    if entry is None and kind == "tools":
        runtime = profile.get("runtime") if isinstance(profile.get("runtime"), dict) else {}
        entry = runtime.get(name)
    if isinstance(entry, dict):
        executable = entry.get("executable") or entry.get("path")
    elif isinstance(entry, str):
        executable = entry
    else:
        executable = None
    if executable:
        return executable
    if bool(profile.get("strict_tool_profile")):
        return None
    return shutil.which(name)


def profile_summary(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = get_tool_profile(state)
    tools = profile.get("tools")
    runtime = profile.get("runtime")
    commands = profile.get("commands")
    return {
        "profile_id": profile.get("profile_id") or DEFAULT_PROFILE_ID,
        "runner": profile.get("runner") or "local_saas",
        "artifact_policy": profile.get("artifact_policy") or DEFAULT_ARTIFACT_POLICY,
        "strict_tool_profile": bool(profile.get("strict_tool_profile")),
        "tools": sorted(tools.keys()) if isinstance(tools, dict) else sorted(tools or []),
        "runtime": sorted(runtime.keys()) if isinstance(runtime, dict) else sorted(runtime or []),
        "commands": sorted(commands.keys()) if isinstance(commands, dict) else sorted(commands or []),
    }


def profile_diagnostics(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = get_tool_profile(state)

    def _section(name: str) -> Dict[str, Any]:
        section = profile.get(name) if isinstance(profile.get(name), dict) else {}
        out: Dict[str, Any] = {}
        for key, value in sorted(section.items()):
            configured = value.get("executable") or value.get("path") if isinstance(value, dict) else value
            resolved = resolve_tool(key, state, kind=name)
            out[key] = {
                "configured": configured or "",
                "resolved": resolved or "",
                "available": bool(resolved and (os.path.basename(resolved) == resolved or os.path.exists(resolved))),
            }
        return out

    return {
        **profile_summary(state),
        "tools": _section("tools"),
        "runtime": _section("runtime"),
        "commands": sorted((profile.get("commands") or {}).keys()) if isinstance(profile.get("commands"), dict) else [],
        "env_keys": sorted((profile.get("env") or {}).keys()) if isinstance(profile.get("env"), dict) else [],
    }
