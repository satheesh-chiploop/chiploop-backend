import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .contracts import RunRequest, RunResult
from .profiles import get_tool_profile, profile_summary, resolve_tool


ToolRunResult = RunResult

TOOL_ALIASES = {
    "cocotb-config": "cocotb_config",
    "python3": "python3",
    "python": "python",
    "pip3": "pip",
    "pytest": "pytest",
    "verilator": "verilator",
    "verilator_coverage": "verilator_coverage",
    "iverilog": "iverilog",
    "vvp": "vvp",
    "yosys": "yosys",
    "sby": "sby",
    "z3": "z3",
    "boolector": "boolector",
    "make": "make",
    "cargo": "cargo",
    "rustc": "rustc",
    "llvm-cov": "llvm_cov",
    "llvm-profdata": "llvm_profdata",
    "gcov": "gcov",
    "git": "git",
    "bash": "bash",
    "scons": "scons",
    "gcc": "gcc",
    "ngspice": "ngspice",
    "riscv64-linux-gnu-gcc": "riscv64_linux_gnu_gcc",
    "riscv64-unknown-elf-gcc": "riscv64_unknown_elf_gcc",
    "x86_64-linux-gnu-gcc": "x86_64_linux_gnu_gcc",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_env(profile: Dict[str, Any], extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    env = os.environ.copy()
    profile_env = profile.get("env") if isinstance(profile.get("env"), dict) else {}
    for key, value in profile_env.items():
        if isinstance(value, dict):
            prepend = value.get("prepend") or []
            append = value.get("append") or []
            parts = [str(x) for x in prepend if x]
            current = env.get(key)
            if current:
                parts.append(current)
            parts.extend(str(x) for x in append if x)
            if parts:
                env[key] = os.pathsep.join(parts)
        elif value is not None:
            env[key] = str(value)
    for key, value in (extra_env or {}).items():
        if value is not None:
            env[key] = str(value)
    return env


def tool_path(name: str, state: Optional[Dict[str, Any]] = None, *, kind: str = "tools") -> Optional[str]:
    return resolve_tool(name, state, kind=kind)


def tool_available(name: str, state: Optional[Dict[str, Any]] = None, *, kind: str = "tools") -> bool:
    path = tool_path(name, state, kind=kind)
    return bool(path and (os.path.basename(path) == path or os.path.exists(path)))


def tool_env(state: Optional[Dict[str, Any]] = None, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return _build_env(get_tool_profile(state or {}), extra_env)


def _result_from_exception(
    state: Optional[Dict[str, Any]],
    capability: str,
    tool: str,
    executable: Optional[str],
    command: List[str],
    cwd: Optional[str],
    exc: Exception,
    started_at: str,
    start: float,
) -> RunResult:
    summary = profile_summary(state or {})
    return RunResult(
        profile_id=str(summary["profile_id"]),
        runner=str(summary["runner"]),
        capability=capability,
        tool=tool,
        executable=executable,
        command=command,
        cwd=cwd,
        returncode=None,
        status="exception",
        error=str(exc),
        started_at=started_at,
        ended_at=_iso_now(),
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


def run_tool(
    state: Optional[Dict[str, Any]],
    capability: str,
    tool: str,
    args: List[str],
    *,
    cwd: Optional[str] = None,
    timeout_sec: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    kind: str = "tools",
    metadata: Optional[Dict[str, Any]] = None,
) -> RunResult:
    state = state or {}
    profile = get_tool_profile(state)
    summary = profile_summary(state)
    executable = resolve_tool(tool, state, kind=kind)
    request = RunRequest(
        capability=capability,
        tool=tool,
        args=list(args),
        cwd=cwd,
        timeout_sec=timeout_sec,
        env=dict(env or {}),
        metadata=dict(metadata or {}),
    )
    command = [executable or tool] + list(args)
    started_at = _iso_now()
    start = time.monotonic()

    if not executable:
        return RunResult(
            profile_id=str(summary["profile_id"]),
            runner=str(summary["runner"]),
            capability=capability,
            tool=tool,
            executable=None,
            command=command,
            cwd=cwd,
            returncode=None,
            status="tool_unavailable",
            error=f"{tool} executable was not found in active ChipLoop tool profile",
            started_at=started_at,
            ended_at=_iso_now(),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
            env=_build_env(profile, request.env),
        )
        status = "success" if proc.returncode == 0 else "failed"
        return RunResult(
            profile_id=str(summary["profile_id"]),
            runner=str(summary["runner"]),
            capability=capability,
            tool=tool,
            executable=executable,
            command=command,
            cwd=cwd,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            status=status,
            started_at=started_at,
            ended_at=_iso_now(),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return _result_from_exception(state, capability, tool, executable, command, cwd, exc, started_at, start)


def run_command(
    state: Optional[Dict[str, Any]],
    capability: str,
    command: List[str],
    *,
    cwd: Optional[str] = None,
    timeout_sec: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> RunResult:
    if not command:
        summary = profile_summary(state or {})
        return RunResult(
            profile_id=str(summary["profile_id"]),
            runner=str(summary["runner"]),
            capability=capability,
            tool="",
            executable=None,
            command=[],
            cwd=cwd,
            returncode=None,
            status="invalid_request",
            error="empty command",
            started_at=_iso_now(),
            ended_at=_iso_now(),
            elapsed_ms=0,
        )

    head = str(command[0])
    alias = TOOL_ALIASES.get(os.path.basename(head), os.path.basename(head))
    if not os.path.isabs(head) and resolve_tool(alias, state or {}):
        return run_tool(
            state,
            capability,
            alias,
            [str(x) for x in command[1:]],
            cwd=cwd,
            timeout_sec=timeout_sec,
            env=env,
            metadata=metadata,
        )

    profile = get_tool_profile(state or {})
    summary = profile_summary(state or {})
    started_at = _iso_now()
    start = time.monotonic()
    command_str = [str(x) for x in command]
    try:
        proc = subprocess.run(
            command_str,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
            env=_build_env(profile, env),
        )
        return RunResult(
            profile_id=str(summary["profile_id"]),
            runner=str(summary["runner"]),
            capability=capability,
            tool=head,
            executable=head,
            command=command_str,
            cwd=cwd,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            status="success" if proc.returncode == 0 else "failed",
            started_at=started_at,
            ended_at=_iso_now(),
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return _result_from_exception(state, capability, head, head, command_str, cwd, exc, started_at, start)
