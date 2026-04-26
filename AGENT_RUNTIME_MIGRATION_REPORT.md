# Agent Runtime Contract v1 Migration Report

## Scope

Backend-only runtime contract migration. No frontend changes, public API route changes, secret changes, environment file changes, deployment changes, DAG changes, or parallel execution changes were made.

## Runtime Contract

Added `agents/runtime.py` with:

- `AgentContext`
- `AgentResult`
- `AgentStatus`
- `ArtifactRef`
- `execute_agent(context, handler)`
- `execute_legacy_agent(agent_fn, context)`

The runtime wrapper logs structured fields for `workflow_id`, `run_id`, `loop_type`, `agent_name`, start/end time, elapsed time, status, artifacts detected in the legacy result, and traceback on failure.

Runtime logging is configured with `configure_runtime_logging()` so known runtime `extra` fields are visible in standard log output.

## Execution Entry Points Mapped

Current backend agent execution is centralized in `main.py` through:

- `_run_nodes_with_shared_state(...)` for app/template runs across digital, analog, embedded/firmware, validation, and system/software workflows.
- `execute_workflow_background(...)` for general workflow execution.
- `execute_validation_run_app_background(...)` direct schematic-agent branch.
- `/validation/test_plan/preview` direct validation test-plan preview call.
- Simulation queue handoff now emits `agent.queue_handoff` with workflow/run/loop/agent context before preserving the existing ChipRunner queue behavior.

All call sites now route legacy `run_agent(state)` functions through `_execute_agent_with_runtime(...)`, which uses `execute_legacy_agent(...)`.

## Agents Migrated

All agents registered in the domain maps now run through Agent Runtime Contract v1 at the executor boundary:

- `DIGITAL_AGENT_FUNCTIONS`
- `ANALOG_AGENT_FUNCTIONS`
- `EMBEDDED_AGENT_FUNCTIONS`
- `VALIDATION_AGENT_FUNCTIONS`
- `SYSTEM_AGENT_FUNCTIONS`
- `AGENT_REGISTRY` custom agents loaded by `load_custom_agents()`

This covers digital, analog, firmware/embedded, validation, system/software, and RTL2GDS/physical-design agents that are executed by the existing workflow maps.

## Direct Shim Agents

These agents also have a local `run_agent(state)` compatibility shim that constructs `AgentContext` directly:

- `agents/digital/digital_rtl_agent.py`
- `agents/analog/analog_exec_summary_agent.py`
- `agents/embedded/embedded_result_agent.py`
- `agents/validation/validation_scope_agent.py`
- `agents/system/system_workflow_agent.py`

## Compatibility Adapter

Most existing agents remain legacy internally and use `execute_legacy_agent(...)` through the executor adapter. This is intentional for minimal risk:

- Existing `run_agent(state) -> dict` behavior is preserved.
- Existing state mutation behavior is preserved.
- Existing Supabase artifact helper calls remain inside agents.
- Existing artifact names, folders, logs, and workflow outputs are not rewritten by the runtime.

## Files Changed

- `agents/runtime.py`
- `main.py`
- `agents/digital/digital_rtl_agent.py`
- `agents/analog/analog_exec_summary_agent.py`
- `agents/embedded/embedded_result_agent.py`
- `agents/validation/validation_scope_agent.py`
- `agents/system/system_workflow_agent.py`
- `tests/test_agent_runtime.py`
- `AGENT_RUNTIME_MIGRATION_REPORT.md`

## Hardening Added

- Duplicate runtime logs are suppressed for the five directly shimmed reference agents when they are invoked through the central executor adapter.
- Direct calls to those reference agents still produce runtime logs.
- The legacy adapter sets and restores a transient `_agent_runtime_active` marker and removes it before returning, so shared workflow state is not polluted.

## Known Risks

- Agents invoked outside `main.py` and outside their own direct shim still depend on callers using `execute_legacy_agent(...)`.
- Full integration testing requires valid Supabase and LLM/Portkey environment configuration; unit tests intentionally avoid secrets and network calls.

## Manual Test Commands

```powershell
python -m py_compile agents\runtime.py main.py
python -m py_compile agents\digital\digital_rtl_agent.py agents\analog\analog_exec_summary_agent.py agents\embedded\embedded_result_agent.py agents\validation\validation_scope_agent.py agents\system\system_workflow_agent.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py
```
