# ChipLoop Studio Contract v1 Migration Report

## Scope

Studio Contract metadata only. No frontend changes, workflow execution changes, agent internal rewrites, secret changes, environment file changes, deployment changes, DAG changes, parallel execution, Codex generation, or IDE/plugin integration were added.

## Files Added

- `studio_contract/__init__.py`
- `studio_contract/specs.py`
- `studio_contract/registry.py`
- `studio_contract/validate_registry.py`
- `registry/agents.yaml`
- `registry/skills.yaml`
- `registry/commands.yaml`
- `registry/hooks.yaml`
- `registry/tools.yaml`
- `registry/workflows.yaml`
- `tests/test_studio_contract_registry.py`
- `STUDIO_CONTRACT_MIGRATION_REPORT.md`

## Files Changed

- No production execution files were changed for Studio Contract v1 metadata.
- Existing Agent Runtime hardening changes on this branch remain separate from the Studio metadata layer.

## Registry Counts

- Agents registered: 151
- Skills registered: 12
- Commands registered: 2
- Hooks registered: 6
- Tools registered: 11
- Workflows registered: 6

## Current Status

- Agents registered: 151
- Skills registered: 12
- Tools/plugins registered: 11
- Hooks registered: 6
- Workflows registered: 6
- Commands registered: 2
- Contract tests passing: 26

## Reference Agent

Reference Studio-compatible agent:

- `Digital RTL Agent`
- Entrypoint: `agents.digital.digital_rtl_agent:run_agent`
- Execution mode: `native`
- Skills include RTL generation, RTL repair, compile/lint check, and artifact publishing.
- Tools include OpenAI/Portkey LLM, Supabase, Python, Icarus Verilog, and Verilator.

## Legacy Adapter Agents

Most registered agents still use `execution_mode: legacy_adapter`. This is intentional:

- Agent internals stay unchanged.
- `run_agent(state) -> dict` compatibility is preserved.
- Existing artifact names, folders, Supabase uploads, logs, and workflow outputs remain agent-owned.
- The Agent Runtime Contract v1 executor adapter remains the compatibility boundary.

## Custom Registry Agents

Dynamic agents loaded into `AGENT_REGISTRY` are represented by the `custom_agent_registry` workflow metadata entry. They cannot be fully statically enumerated until they are present as repo files or persisted registry rows, but they remain runtime-compatible through the existing executor adapter.

## Validation

Dry-run registry validation checks:

- Required agent fields
- Entrypoint module path and function presence without importing agent modules
- Valid `execution_mode`
- Agent skill references
- Agent tool references
- Agent hook references
- Command agent references
- Workflow agent and hook references

Manual commands:

```powershell
python -m studio_contract.validate_registry --registry-dir registry
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_studio_contract_registry.py
```

## Risks

- Registry metadata is inferred from current `main.py` maps and `agent_capabilities.py`; some descriptions and input/output patterns are best-effort where capability metadata is absent.
- `.yaml` registry files are JSON-compatible YAML and intentionally loaded with Python stdlib JSON to avoid adding dependencies.
- Tool dependencies remain metadata declarations for default registry validation; optional local availability reporting is available separately.
- Workflow registry entries model existing loop-level agent registries, not actual Studio DAG execution.

## Next Migration Plan

1. Promote selected high-value agents from `legacy_adapter` to richer native Studio specs as their internals are migrated.
2. Review custom-agent metadata exports from `generated_patches/` before manually materializing dynamic `AGENT_REGISTRY` entries into registry files.
3. Add workflow template metadata once Studio DAG contracts are ready.

## Phase 3: Codex Studio Agent Factory

Implemented as standalone developer tooling:

- `studio_planner` plans reuse, extension, composition, or creation from registry metadata.
- `studio_factory` generates reviewable agent stubs and registry patches only in safe generated directories.
- Default mode is dry-run.
- Write mode requires `--write` and rejects production paths.

CLI examples:

```powershell
python -m studio_planner.plan_agent --request examples/studio_planner/request.json
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --dry-run
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --write --output-dir .
python -m studio_contract.validate_tools --registry-dir registry
python -m studio_factory.export_custom_agents --input examples/studio_factory/custom_agents_export_input.json --write
```

Phase 3 tests:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_studio_planner_factory.py
```
