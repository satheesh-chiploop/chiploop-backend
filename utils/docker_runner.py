import os
import subprocess
from typing import Dict, List, Optional, Tuple

def run_docker(
    image: str,
    cmd: List[str],
    mounts: Optional[List[Tuple[str, str]]] = None,  # (host_path, container_path)
    env: Optional[Dict[str, str]] = None,
    workdir: Optional[str] = None,
    timeout_sec: int = 60 * 60,
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

    p = subprocess.run(
        docker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout