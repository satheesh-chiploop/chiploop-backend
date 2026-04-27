# ChipLoop Runtime and Studio Contracts

This document describes the Phase 1 Agent Runtime Contract v1 and Phase 2 Studio Contract v1 metadata layer.

No frontend behavior, public workflow API behavior, Supabase artifact behavior, or production workflow execution behavior is changed by these contracts.

## Current Status

- Agents registered: 151
- Skills registered: 12
- Tools/plugins registered: 11
- Hooks registered: 6
- Workflows registered: 6
- Commands registered: 2
- Contract tests passing: 26

## Phase 1: Agent Runtime Contract v1

Agent Runtime Contract v1 lives in `agents/runtime.py`.

It standardizes how backend agents are executed and observed while preserving legacy agent internals. Most existing agents still expose the legacy shape:

```python
def run_agent(state: dict) -> dict:
    ...
    return state
```

The runtime layer wraps this shape instead of rewriting all agents.

### AgentContext

`AgentContext` is the runtime input model. It carries:

- `agent_name`
- mutable legacy `state`
- `workflow_id`
- `run_id`
- `loop_type`
- `artifact_dir`
- `user_id`
- runtime metadata

The shared mutable state remains the compatibility surface for existing workflows.

### AgentResult

`AgentResult` is the normalized runtime output model. It captures:

- legacy status text
- normalized runtime status
- state data updates
- artifact metadata when returned by agents
- log/code fields where present
- start/end timestamps
- elapsed milliseconds
- raw legacy result

For legacy agents, `AgentResult.raw` preserves the original dict so existing `shared_state.update(result)` behavior remains unchanged.

### execute_agent

`execute_agent(context, handler)` is the native runtime wrapper. It logs:

- `workflow_id`
- `run_id`
- `loop_type`
- `agent_name`
- start time
- end time
- elapsed time
- status
- artifact summary
- traceback on failure

It does not upload artifacts. Existing agents continue to own Supabase artifact publishing through their current helper calls.

### execute_legacy_agent

`execute_legacy_agent(agent_fn, context)` adapts legacy `run_agent(state)` functions to the runtime contract.

The adapter:

- calls the existing agent with the same mutable `state`
- preserves the returned legacy dict
- does not rename, move, upload, or rewrite artifacts
- sets a transient `_agent_runtime_active` marker to avoid duplicate runtime logs for locally shimmed agents
- removes or restores that marker before returning

### Runtime Validation

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py
```

## Phase 2: Studio Contract v1

Studio Contract v1 is a metadata layer for ChipLoop Studio. It does not currently drive workflow execution.

Code lives in:

- `studio_contract/specs.py`
- `studio_contract/registry.py`
- `studio_contract/validate_registry.py`

Registry files live in `registry/`.

### Spec Models

Studio Contract v1 defines:

- `AgentSpec`
- `SkillSpec`
- `CommandSpec`
- `HookSpec`
- `ToolSpec`
- `WorkflowSpec`

These models describe what exists today so Studio can reason over agents, skills, tools, hooks, commands, and workflows without changing runtime execution.

## Registry Files

The registry files are JSON-compatible YAML and are loaded with Python stdlib JSON to avoid adding a YAML dependency.

### registry/agents.yaml

Contains one `AgentSpec` per registered backend agent.

Each agent declares:

- `name`
- `loop_type`
- `domain`
- `description`
- `entrypoint`
- `execution_mode`
- `inputs`
- `outputs`
- `artifact_paths`
- `artifact_types`
- `required_skills`
- `required_tools`
- `hooks`

Execution modes:

- `native`: agent has a direct runtime-compatible shim.
- `legacy_adapter`: agent is still internally legacy but runs through Agent Runtime Contract v1.

### registry/skills.yaml

Reusable Studio capabilities, including:

- `spec_ingest`
- `rtl_generation`
- `rtl_repair`
- `rtl_compile_check`
- `cocotb_testbench_generation`
- `firmware_manifest_generation`
- `register_map_extraction`
- `software_sdk_generation`
- `openlane_config_generation`
- `sta_constraint_generation`
- `artifact_publish`
- `supabase_handoff_ingest`

### registry/tools.yaml

Existing external tools and services:

- `openai_or_portkey_llm`
- `supabase`
- `python`
- `iverilog`
- `verilator`
- `cocotb`
- `yosys`
- `openlane`
- `openroad`
- `cargo`
- `git`

These are metadata declarations only. The current validator does not check local tool installation.

### registry/hooks.yaml

Common lifecycle hooks:

- `pre_run_validate_inputs`
- `post_run_collect_artifacts`
- `post_run_update_state`
- `on_failure_capture_traceback`
- `on_failure_preserve_logs`
- `artifact_publish_to_supabase`

### registry/commands.yaml

Command metadata. Current entries:

- `digital_rtl.generate`
- `registry.validate`

### registry/workflows.yaml

Loop-level workflow metadata for existing registered agents:

- digital
- analog
- embedded
- validation
- system
- custom registry metadata

These are metadata groupings, not Studio DAG execution definitions.

## Registry Validation

Dry-run validation checks:

- every agent has required fields
- entrypoint module path and function are present without importing agent modules
- execution mode is valid
- referenced skills exist
- referenced tools exist
- referenced hooks exist
- command agent references exist
- workflow agent references exist

Run:

```powershell
python -m studio_contract.validate_registry --registry-dir registry
```

Optional local tool availability report:

```powershell
python -m studio_contract.validate_tools --registry-dir registry
```

Use `--strict` only when a CI or worker image should fail on missing required tools.

Run tests:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_studio_contract_registry.py
```

Run all current contract tests:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py tests/test_studio_contract_registry.py tests/test_studio_planner_factory.py
```

## Adding a New Agent to the Registry

1. Add the backend agent implementation with a stable `run_agent(state)` entrypoint.
2. Register it in the appropriate backend domain map in `main.py`.
3. Add an entry to `registry/agents.yaml`.
4. Set `execution_mode`:
   - use `legacy_adapter` for normal legacy `run_agent(state)` agents
   - use `native` only when the agent has an explicit runtime-compatible shim
5. Declare required skills from `registry/skills.yaml`.
6. Declare required tools from `registry/tools.yaml`.
7. Include the standard hooks unless there is a clear reason not to:
   - `pre_run_validate_inputs`
   - `post_run_collect_artifacts`
   - `post_run_update_state`
   - `on_failure_capture_traceback`
   - `on_failure_preserve_logs`
   - `artifact_publish_to_supabase`
8. Add the agent name to the relevant workflow entry in `registry/workflows.yaml`.
9. Run:

```powershell
python -m studio_contract.validate_registry --registry-dir registry
```

## Phase 3: Codex Studio Agent Factory

Phase 3 adds standalone developer tools for planning and safely generating reviewable agent stubs. It does not change existing app workflows or production execution.

Planner CLI:

```powershell
python -m studio_planner.plan_agent --request examples/studio_planner/request.json
```

Factory dry-run CLI:

```powershell
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --dry-run
```

Factory write mode:

```powershell
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --write --output-dir .
```

Custom-agent metadata export:

```powershell
python -m studio_factory.export_custom_agents --input examples/studio_factory/custom_agents_export_input.json --write
```

This export writes review-only metadata patches and does not query Supabase or modify production registries.

The factory writes only to safe generated directories:

- `generated_studio_agents/`
- `generated_patches/`
- `examples/studio_factory/`

It never writes into production paths such as `agents/`, `registry/`, or `main.py`.

See `PHASE3_CODEX_STUDIO_AGENT_FACTORY.md` for full details.

## Next Phase After Factory v1

Recommended follow-up scope:

1. Review generated factory outputs before promotion.
2. Review custom-agent metadata patches before manually merging any dynamic agents.
3. Add workflow-template metadata once Studio DAG contracts are ready.
