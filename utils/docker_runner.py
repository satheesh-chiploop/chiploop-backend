import os
from typing import Dict, List, Optional, Tuple

from tooling.runner import run_command

def run_docker(
    image: str,
    cmd: List[str],
    mounts: Optional[List[Tuple[str, str]]] = None,  # (host_path, container_path)
    env: Optional[Dict[str, str]] = None,
    workdir: Optional[str] = None,
    timeout_sec: int = 60 * 60,
    state: Optional[Dict] = None,
) -> Tuple[int, str]:
    """
    Minimal, deterministic docker runner.
    Returns (exit_code, combined_output).
    """
    docker_cmd = ["docker", "run", "--rm"]

    if mounts:
        for host_path, container_path in mounts:
            docker_cmd += ["-v", f"{host_path}:{container_path}"]

    if env:
        for k, v in env.items():
            docker_cmd += ["-e", f"{k}={v}"]

    if workdir:
        docker_cmd += ["-w", workdir]

    docker_cmd += [image] + cmd

    result = run_command(state or {}, "docker_run", docker_cmd, timeout_sec=timeout_sec)
    output = "\n".join(part for part in [result.stdout, result.stderr, result.error] if part)
    return result.returncode if result.returncode is not None else 1, output
