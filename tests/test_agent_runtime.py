import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.runtime import AgentContext, AgentResult, AgentStatus, execute_agent, execute_legacy_agent


def test_agent_context_from_state_keeps_legacy_state_reference():
    state = {
        "workflow_id": "wf-1",
        "run_id": "run-1",
        "loop_type": "digital",
        "artifact_dir": "artifacts/u/wf-1/run-1",
        "user_id": "user-1",
    }

    context = AgentContext.from_state(state, "Example Agent")

    assert context.agent_name == "Example Agent"
    assert context.workflow_id == "wf-1"
    assert context.run_id == "run-1"
    assert context.loop_type == "digital"
    assert context.state is state


def test_execute_agent_normalizes_agent_result(caplog):
    state = {"workflow_id": "wf-2"}
    context = AgentContext.from_state(state, "Example Agent")

    def handler(ctx):
        return AgentResult(status="ok", data={"value": 42})

    with caplog.at_level(logging.INFO, logger="chiploop.agent_runtime"):
        result = execute_agent(context, handler)

    assert result.to_state_update() == {"value": 42, "status": "ok"}
    assert result.started_at
    assert result.ended_at
    assert result.elapsed_ms is not None
    assert any(record.message == "agent.start" and record.agent_name == "Example Agent" for record in caplog.records)
    assert any(record.message == "agent.complete" and record.agent_name == "Example Agent" for record in caplog.records)


def test_execute_agent_normalizes_legacy_dict_return():
    state = {"workflow_id": "wf-3"}
    context = AgentContext.from_state(state, "Legacy Agent")

    result = execute_agent(context, lambda ctx: {"status": "done", "artifact": "path.txt"})

    assert result.status == "done"
    assert result.data["artifact"] == "path.txt"
    assert result.raw == {"status": "done", "artifact": "path.txt"}
    assert result.runtime_status == AgentStatus.SUCCESS


def test_execute_legacy_agent_adapter_preserves_state_return():
    state = {"workflow_id": "wf-legacy", "count": 1}
    context = AgentContext.from_state(state, "Legacy Agent")

    def legacy_agent(legacy_state):
        legacy_state["count"] += 1
        legacy_state["status"] = "✅ legacy complete"
        legacy_state["artifact"] = "artifact.txt"
        return legacy_state

    result = execute_legacy_agent(legacy_agent, context)

    assert state["count"] == 2
    assert result.raw is not None
    assert result.raw["artifact"] == "artifact.txt"
    assert result.runtime_status == AgentStatus.SUCCESS


@pytest.mark.parametrize("loop_type", ["digital", "analog", "embedded", "validation", "system"])
def test_execute_legacy_agent_adapter_smoke_by_loop(loop_type):
    state = {"workflow_id": f"wf-{loop_type}", "run_id": f"run-{loop_type}", "loop_type": loop_type}
    context = AgentContext.from_state(state, f"{loop_type.title()} Smoke Agent")

    def legacy_agent(legacy_state):
        legacy_state["status"] = "done"
        legacy_state[f"{loop_type}_smoke"] = True
        return legacy_state

    result = execute_legacy_agent(legacy_agent, context)

    assert result.runtime_status == AgentStatus.SUCCESS
    assert result.raw[f"{loop_type}_smoke"] is True
    assert context.loop_type == loop_type


def test_execute_agent_logs_and_reraises_failures(caplog):
    context = AgentContext.from_state({"workflow_id": "wf-4"}, "Failing Agent")

    def handler(ctx):
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="chiploop.agent_runtime"):
        with pytest.raises(RuntimeError, match="boom"):
            execute_agent(context, handler)

    assert any(record.message == "agent.failed" and record.agent_name == "Failing Agent" for record in caplog.records)
    assert any(getattr(record, "error_traceback", "") for record in caplog.records)
