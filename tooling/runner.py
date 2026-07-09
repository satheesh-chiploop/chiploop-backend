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
    "verible-verilog-lint": "verible_verilog_lint",
    "verible-verilog-syntax": "verible_verilog_syntax",
    "slang": "slang",
    "sv2v": "sv2v",
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
    "docker": "docker",
    "openroad": "openroad",
    "sta": "sta",
    "opensta": "sta",
    "fault": "fault",
    "atalanta": "atalanta",
    "podem": "podem",
    "openram": "openram",
    "autombist": "autombist",
    "dc_shell": "synopsys_dc",
    "synopsys_dc": "synopsys_dc",
    "genus": "cadence_genus",
    "cadence_genus": "cadence_genus",
    "xrun": "xcelium",
    "xcelium": "xcelium",
    "vcs": "vcs",
    "vsim": "questa",
    "questa": "questa",
    "vc_static_shell": "vc_lp",
    "vcst": "vc_lp",
    "vc_lp": "vc_lp",
    "jaspergold": "jasper",
    "jasper": "jasper",
    "riscv64-linux-gnu-gcc": "riscv64_linux_gnu_gcc",
    "riscv64-unknown-elf-gcc": "riscv64_unknown_elf_gcc",
    "x86_64-linux-gnu-gcc": "x86_64_linux_gnu_gcc",
}

VERSION_ARGS = {
    "python": ["--version"],
    "python3": ["--version"],
    "pip": ["--version"],
    "pytest": ["--version"],
    "cocotb_config": ["--version"],
    "verilator": ["--version"],
    "verilator_coverage": ["--version"],
    "verible_verilog_lint": ["--version"],
    "verible_verilog_syntax": ["--version"],
    "slang": ["--version"],
    "sv2v": ["--version"],
    "iverilog": ["-V"],
    "vvp": ["-V"],
    "yosys": ["-V"],
    "sby": ["--version"],
    "z3": ["--version"],
    "boolector": ["--version"],
    "make": ["--version"],
    "cargo": ["--version"],
    "rustc": ["--version"],
    "git": ["--version"],
    "gcc": ["--version"],
    "ngspice": ["--version"],
    "docker": ["--version"],
    "openroad": ["-version"],
    "sta": ["-version"],
    "fault": ["--help"],
    "atalanta": ["-h"],
    "podem": ["-h"],
    "openram": ["--help"],
    "autombist": ["--help"],
    "synopsys_dc": ["-version"],
    "cadence_genus": ["-version"],
    "xcelium": ["-version"],
    "vcs": ["-ID"],
    "questa": ["-version"],
    "vc_lp": ["-version"],
    "jasper": ["-version"],
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
    for section_name in ("runtime", "tools"):
        section = profile.get(section_name) if isinstance(profile.get(section_name), dict) else {}
        for name, entry in section.items():
            executable = entry.get("executable") or entry.get("path") if isinstance(entry, dict) else entry
            if executable:
                env_key = "CHIPLOOP_" + str(name).upper().replace("-", "_")
                env.setdefault(env_key, str(executable))
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
    profile = get_tool_profile(state or {})
    resolved = resolve_tool(alias, state or {})
    if not os.path.isabs(head) and resolved:
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

    summary = profile_summary(state or {})
    if not os.path.isabs(head) and bool(profile.get("strict_tool_profile")):
        return RunResult(
            profile_id=str(summary["profile_id"]),
            runner=str(summary["runner"]),
            capability=capability,
            tool=alias,
            executable=None,
            command=[str(x) for x in command],
            cwd=cwd,
            returncode=None,
            status="tool_unavailable",
            error=f"{alias} is not configured in strict ChipLoop tool profile",
            started_at=_iso_now(),
            ended_at=_iso_now(),
            elapsed_ms=0,
        )

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


def run_tool_diagnostics(
    state: Optional[Dict[str, Any]] = None,
    *,
    tools: Optional[List[str]] = None,
    timeout_sec: int = 15,
) -> Dict[str, Any]:
    state = state or {}
    profile = get_tool_profile(state)
    configured = profile.get("tools") if isinstance(profile.get("tools"), dict) else {}
    runtime = profile.get("runtime") if isinstance(profile.get("runtime"), dict) else {}
    names = tools or sorted(set(configured.keys()) | set(runtime.keys()))
    results: Dict[str, Any] = {}

    for name in names:
        kind = "runtime" if name in runtime else "tools"
        executable = resolve_tool(name, state, kind=kind)
        if not executable:
            results[name] = {
                "status": "tool_unavailable",
                "available": False,
                "executable": "",
                "output": "",
            }
            continue
        args = VERSION_ARGS.get(name)
        if not args:
            results[name] = {
                "status": "available",
                "available": True,
                "executable": executable,
                "output": "Version probe not configured.",
            }
            continue
        result = run_tool(state, f"diagnostic.{name}", name, args, timeout_sec=timeout_sec, kind=kind)
        output = (result.stdout or result.stderr or result.error or "").strip()
        results[name] = {
            "status": result.status,
            "available": result.status == "success",
            "executable": result.executable or executable,
            "returncode": result.returncode,
            "output": output[:1000],
        }

    return {
        "profile": profile_summary(state),
        "results": results,
    }
