# Phase 3: Codex Studio Agent Planner and Agent Factory v1

Phase 3 adds standalone planning and safe generation utilities for future Studio agent creation.

This phase does not change frontend behavior, the System Planner, Spec Analyzer, workflow execution, existing agents, production registry files, or production agent paths.

## App vs Studio Separation

Existing app workflows keep using the current backend execution path:

- Studio/app workflow definitions resolve registered backend agents.
- Agents execute through Agent Runtime Contract v1.
- Supabase artifact behavior remains owned by the agents.

The new Phase 3 modules are sidecar developer tools:

- `studio_planner` reads registry metadata and recommends reuse, extension, composition, or creation.
- `studio_factory` creates reviewable generated files only in safe generated directories.
- `studio_contract.validate_tools` optionally reports local tool availability without changing registry validation defaults.

## Agent Planner

Package:

- `studio_planner/models.py`
- `studio_planner/registry_matcher.py`
- `studio_planner/planner.py`
- `studio_planner/plan_agent.py`

The planner accepts:

- `natural_language_request`
- optional `domain`
- optional `loop_type`
- optional `inputs`
- optional `outputs`

It loads the Studio Contract registries and performs deterministic matching:

- normalized name match
- token overlap
- domain match
- loop type match
- skill overlap
- keyword match in descriptions

It returns:

- requested capability
- exact matches
- similar matches
- reusable skills
- reusable tools
- reusable hooks
- missing capabilities
- recommendation
- confidence score
- explanation

Run:

```powershell
python -m studio_planner.plan_agent --request examples/studio_planner/request.json
```

## Agent Factory

Package:

- `studio_factory/models.py`
- `studio_factory/registry_lookup.py`
- `studio_factory/planner.py`
- `studio_factory/generator.py`
- `studio_factory/patch_writer.py`
- `studio_factory/validator.py`
- `studio_factory/generate_agent.py`

The factory is safe by default.

Default mode:

```powershell
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --dry-run
```

Write mode:

```powershell
python -m studio_factory.generate_agent --request examples/studio_factory/system_sta_constraint_agent_request.json --write --output-dir .
```

## Registry Lookup Logic

Before generation, the factory:

1. Searches `registry/agents.yaml`.
2. If an exact match exists, returns `reuse_existing` and stops.
3. If similar matches exist, returns `extend_or_create_variant` unless `allow_extension` is true.
4. If no suitable match exists, plans `create_new`.

No registry file is modified by the factory.

## Dry-Run vs Write Mode

Dry-run:

- default mode
- produces a plan only
- writes no files

Write mode:

- requires `--write`
- writes only review artifacts
- rejects unsafe output paths

Allowed write roots:

- `generated_studio_agents/`
- `generated_patches/`
- `examples/studio_factory/`

Blocked write paths include:

- `agents/`
- `registry/`
- `main.py`
- production execution code paths

## Generated File Locations

For a new agent, the factory can generate:

- agent stub file
- basic test stub
- README / migration notes
- proposed registry patch YAML

Generated agents follow Agent Runtime Contract v1 and retain `run_agent(state)` compatibility.

## Tool Availability Validation

Registry validation checks metadata consistency. Tool availability is intentionally separate because developer machines and production workers may have different optional EDA tools installed.

Report-only mode:

```powershell
python -m studio_contract.validate_tools --registry-dir registry
```

Strict mode returns nonzero only when a required tool is unavailable:

```powershell
python -m studio_contract.validate_tools --registry-dir registry --strict
```

Optional EDA tools such as `iverilog`, `verilator`, `yosys`, `openlane`, and `openroad` are reported as missing when absent, but they do not fail report-only validation.

## Custom-Agent Metadata Export

Dynamic custom agents loaded through `AGENT_REGISTRY` remain existing runtime behavior, not Studio Factory generation behavior. Phase 3 adds a review-only exporter that accepts an offline JSON export and produces proposed Studio registry metadata under `generated_patches/`.

Dry-run:

```powershell
python -m studio_factory.export_custom_agents --input examples/studio_factory/custom_agents_export_input.json --dry-run
```

Write review patch:

```powershell
python -m studio_factory.export_custom_agents --input examples/studio_factory/custom_agents_export_input.json --write
```

The exporter does not query Supabase, read secrets, or modify production registry files.

## Reviewing and Promoting Generated Agents

Generated outputs are review artifacts only.

Promotion steps:

1. Review generated agent code.
2. Implement TODO sections.
3. Confirm inputs, outputs, and artifact behavior.
4. Run generated tests.
5. Dry-run validate proposed registry patch.
6. Manually move reviewed code into production paths.
7. Manually merge registry patch after review.
8. Run full contract tests.

## Tests

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_studio_planner_factory.py
```

Run all contract tests:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py tests/test_studio_contract_registry.py tests/test_studio_planner_factory.py
```
