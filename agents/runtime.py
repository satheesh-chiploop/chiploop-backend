import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from time import perf_counter
from typing import Any, Callable, Dict, List, Mapping, Optional


logger = logging.getLogger("chiploop.agent_runtime")
RUNTIME_ACTIVE_STATE_KEY = "_agent_runtime_active"
RUNTIME_LOG_FIELDS = (
    "workflow_id",
    "run_id",
    "loop_type",
    "agent_name",
    "start_time",
    "end_time",
    "elapsed_ms",
    "status",
    "runtime_status",
    "artifacts_produced",
    "error_traceback",
)


class RuntimeExtraFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts = []
        for key in RUNTIME_LOG_FIELDS:
            value = getattr(record, key, None)
            if value not in (None, "", {}, []):
                parts.append(f"{key}={value}")
        if parts:
            return f"{base} {' '.join(parts)}"
        return base


def configure_runtime_logging() -> None:
    """
    Make Agent Runtime `extra` fields visible on existing logging handlers.

    This only changes formatter output; it does not alter workflow execution,
    artifact storage, or log persistence behavior.
    """
    formatter = RuntimeExtraFormatter("%(levelname)s:%(name)s:%(message)s")
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logger.setLevel(logging.INFO)


class AgentStatus(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class ArtifactRef:
    name: str
    path: str
    kind: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    """
    Agent Runtime Contract v1 context.

    The legacy mutable state dict remains the compatibility surface. New agents
    can consume typed fields while existing agents keep receiving the same state.
    """

    agent_name: str
    state: Dict[str, Any]
    workflow_id: str = "default"
    run_id: Optional[str] = None
    loop_type: Optional[str] = None
    artifact_dir: Optional[str] = None
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None

    @classmethod
    def from_state(
        cls,
        state: Dict[str, Any],
        agent_name: str,
        **metadata: Any,
    ) -> "AgentContext":
        return cls(
            agent_name=agent_name,
            state=state,
            workflow_id=str(state.get("workflow_id") or "default"),
            run_id=state.get("run_id"),
            loop_type=state.get("loop_type"),
            artifact_dir=state.get("artifact_dir"),
            user_id=state.get("user_id"),
            metadata={k: v for k, v in metadata.items() if v is not None},
        )


@dataclass
class AgentResult:
    """
    Agent Runtime Contract v1 result.

    `data` is merged into the legacy shared state by migrated agents. `raw`
    preserves legacy dict returns when execute_agent wraps an unmigrated agent.
    """

    status: Optional[str] = None
    runtime_status: AgentStatus = AgentStatus.UNKNOWN
    data: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    artifact_refs: List[ArtifactRef] = field(default_factory=list)
    log: Optional[str] = None
    code: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    elapsed_ms: Optional[int] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_legacy(cls, value: Any) -> "AgentResult":
        if isinstance(value, AgentResult):
            return value
        if isinstance(value, Mapping):
            raw = dict(value)
            runtime_status = _status_from_value(raw.get("status"))
            return cls(
                status=raw.get("status"),
                runtime_status=runtime_status,
                data=raw,
                artifacts=raw.get("artifacts") if isinstance(raw.get("artifacts"), dict) else {},
                log=raw.get("log"),
                code=raw.get("code"),
                raw=raw,
            )
        return cls(status=str(value) if value is not None else None)

    def to_state_update(self) -> Dict[str, Any]:
        update = dict(self.data)
        if self.status is not None:
            update["status"] = self.status
        if self.artifacts:
            update["artifacts"] = self.artifacts
        if self.log is not None:
            update["log"] = self.log
        if self.code is not None:
            update["code"] = self.code
        return update


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_from_value(value: Any) -> AgentStatus:
    text = str(value or "").strip().lower()
    if not text:
        return AgentStatus.UNKNOWN
    if text.startswith("❌") or "failed" in text or "error" in text or "missing" in text:
        return AgentStatus.FAILED
    if text.startswith("⚠") or "warning" in text or "partial" in text or "with issues" in text:
        return AgentStatus.WARNING
    if text in {"ok", "done", "passed", "pass"}:
        return AgentStatus.SUCCESS
    if text.startswith("✅") or "complete" in text or "generated" in text or "validated" in text or "success" in text:
        return AgentStatus.SUCCESS
    return AgentStatus.UNKNOWN


def _artifact_summary(result: AgentResult) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if result.artifacts:
        out["artifacts"] = sorted(result.artifacts.keys())
    if result.artifact_refs:
        out["artifact_refs"] = [ref.path for ref in result.artifact_refs]
    if isinstance(result.raw, dict):
        for key in ("artifact", "artifact_list", "artifact_log", "rtl_files", "result_file"):
            value = result.raw.get(key)
            if value:
                out[key] = value
    return out


def execute_agent(
    context: AgentContext,
    handler: Callable[[AgentContext], Any],
) -> AgentResult:
    """
    Execute an agent under the v1 runtime contract.

    This wrapper intentionally does not upload or rewrite artifacts. Agents keep
    using existing Supabase artifact helpers so production storage behavior stays
    unchanged during incremental migration.
    """
    start = perf_counter()
    started_at = _utc_now()
    context.started_at = started_at
    logger.info(
        "agent.start",
        extra={
            "workflow_id": context.workflow_id,
            "run_id": context.run_id,
            "loop_type": context.loop_type,
            "agent_name": context.agent_name,
            "start_time": started_at,
        },
    )
    try:
        result = AgentResult.from_legacy(handler(context))
        result.started_at = result.started_at or started_at
        result.ended_at = result.ended_at or _utc_now()
        elapsed_ms = int((perf_counter() - start) * 1000)
        result.elapsed_ms = elapsed_ms
        if result.runtime_status == AgentStatus.UNKNOWN:
            result.runtime_status = _status_from_value(result.status)
        logger.info(
            "agent.complete",
            extra={
                "workflow_id": context.workflow_id,
                "run_id": context.run_id,
                "loop_type": context.loop_type,
                "agent_name": context.agent_name,
                "start_time": result.started_at,
                "end_time": result.ended_at,
                "elapsed_ms": elapsed_ms,
                "status": result.status,
                "runtime_status": result.runtime_status.value,
                "artifacts_produced": _artifact_summary(result),
            },
        )
        return result
    except Exception:
        elapsed_ms = int((perf_counter() - start) * 1000)
        ended_at = _utc_now()
        tb = traceback.format_exc()
        logger.exception(
            "agent.failed",
            extra={
                "workflow_id": context.workflow_id,
                "run_id": context.run_id,
                "loop_type": context.loop_type,
                "agent_name": context.agent_name,
                "start_time": started_at,
                "end_time": ended_at,
                "elapsed_ms": elapsed_ms,
                "status": AgentStatus.FAILED.value,
                "error_traceback": tb,
            },
        )
        raise


def log_runtime_event(
    context: AgentContext,
    event: str,
    status: Optional[Any] = None,
    artifacts_produced: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> None:
    extra = {
        "workflow_id": context.workflow_id,
        "run_id": context.run_id,
        "loop_type": context.loop_type,
        "agent_name": context.agent_name,
        "status": status.value if isinstance(status, AgentStatus) else status,
        "artifacts_produced": artifacts_produced or {},
    }
    extra.update({k: v for k, v in fields.items() if v is not None})
    logger.info(event, extra=extra)


def execute_legacy_agent(
    agent_fn: Callable[[Dict[str, Any]], Any],
    context: AgentContext,
) -> AgentResult:
    """
    Compatibility adapter for legacy ChipLoop agents with run_agent(state).

    Existing agents still mutate and return the shared state dict. This adapter
    routes them through execute_agent without changing their artifact uploads,
    local files, logs, or public return shape.
    """

    def _handler(ctx: AgentContext) -> Any:
        marker_was_present = RUNTIME_ACTIVE_STATE_KEY in ctx.state
        previous_marker = ctx.state.get(RUNTIME_ACTIVE_STATE_KEY)
        ctx.state[RUNTIME_ACTIVE_STATE_KEY] = True
        try:
            return agent_fn(ctx.state)
        finally:
            if marker_was_present:
                ctx.state[RUNTIME_ACTIVE_STATE_KEY] = previous_marker
            else:
                ctx.state.pop(RUNTIME_ACTIVE_STATE_KEY, None)

    return execute_agent(context, _handler)
