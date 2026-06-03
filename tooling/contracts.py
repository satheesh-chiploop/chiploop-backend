import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


DEFAULT_ARTIFACT_POLICY = "full_sync"
DEFAULT_PROFILE_ID = "chiploop_saas_default"


@dataclass
class RunRequest:
    capability: str
    tool: str
    args: List[str]
    cwd: Optional[str] = None
    timeout_sec: Optional[int] = None
    env: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return data


@dataclass
class RunnerJob:
    job_id: str
    workflow_id: str
    capability: str
    adapter: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    expected_outputs: List[str] = field(default_factory=list)
    tool_profile_id: Optional[str] = None
    model_profile_id: Optional[str] = None
    artifact_policy: str = DEFAULT_ARTIFACT_POLICY
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return data


@dataclass
class ToolAdapter:
    adapter_id: str
    capability: str
    tool: str
    description: str
    command_template: List[str]
    expected_outputs: List[str] = field(default_factory=list)
    log_patterns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    profile_id: str
    runner: str
    capability: str
    tool: str
    executable: Optional[str]
    command: List[str]
    cwd: Optional[str]
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    status: str = "unknown"
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    elapsed_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
