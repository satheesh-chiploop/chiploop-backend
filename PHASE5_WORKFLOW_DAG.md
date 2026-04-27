# Phase 5: Workflow DAG Opt-In Execution

Phase 5 introduces a production-safe DAG foundation for future parallel workflow execution. Existing App workflows continue to use the current serial executor by default.

## Why DAG

Serial workflows are simple and safe, but some ChipLoop stages can run independently after a shared prerequisite. For example, once RTL exists, simulation preview, firmware preparation, and software SDK preparation can be planned as parallel branches before final integration.

## Current System Planner Assumptions

The current System Planner emits ordered agent lists or Studio-style `nodes` and `edges`. Existing execution assumes a strict serial path:

- `plan_workflow()` returns `{"loop_type": ..., "agents": [...]}`.
- `auto_compose_workflow_graph()` converts selected agents into linear nodes and edges.
- Existing backend execution topologically sorts graph nodes, then executes agents one at a time.

Dependencies are currently implied by order. Phase 5 keeps that behavior and adds optional DAG metadata only when explicitly requested.

## New Package

`workflow_dag/` contains:

- `models.py`: typed DAG nodes, edges, state, and results.
- `validator.py`: cycle detection, registry agent checks, dependency checks, orphan checks, and input satisfaction checks.
- `planner.py`: serial-plan to DAG conversion and dry-run grouping.
- `executor.py`: opt-in ThreadPoolExecutor-based DAG executor using Agent Runtime Contract v1.
- `state_store.py`: locked shared-state merge helper.
- `examples.py`: CLI preview for example DAGs.

## Serial vs DAG Mode

Default remains serial.

Optional System Planner DAG output:

```json
{
  "workflow_mode": "dag",
  "nodes": [],
  "edges": [],
  "dag_preview": {}
}
```

The planner does not execute DAGs. It only emits graph metadata.

## Dry Run

Preview the example DAG without executing agents:

```powershell
python -m workflow_dag.examples --dry-run
```

The dry run shows:

- execution order
- parallel groups
- dependency graph
- parallel group agent names

## Example

`examples/dag/arch2rtl_dag_preview.json` models:

```text
Spec ingest
  -> RTL generation
    -> Simulation
    -> Firmware prep
    -> Software prep
  -> Final integration
```

## Failure Policies

Supported per-node policies:

- `fail_fast`: stop scheduling downstream work on failure.
- `continue_on_failure`: record failure and continue unrelated work.
- `skip_dependents`: skip nodes that depend on the failed node.

## Safety

- DAG execution is opt-in only.
- Existing App routes and serial workflow execution are unchanged.
- Agents are not rewritten.
- Supabase artifact behavior remains agent-owned.
- No frontend changes are required.
- No SDK behavior changes are required.

## Limitations

- The DAG executor is a foundation and is not wired into the default App execution path.
- Shared-state merging is conservative and key-based; agents with hidden write conflicts should stay serial.
- Parallel opportunity detection is heuristic and should be reviewed before promotion to production execution.
- Timeouts depend on Python future behavior and cannot forcibly stop already-running agent code.
