import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .contracts import RunRequest, RunResult
from .profiles import get_tool_profile, profile_summary, resolve_tool


ToolRunResult = RunResult


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
