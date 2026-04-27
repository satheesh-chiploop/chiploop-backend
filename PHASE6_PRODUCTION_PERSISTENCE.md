# Phase 6: Production Persistence

Phase 6 prepares production persistence for SDK/API keys, usage tracking, optional structured workflow events, and future plans/credits.

No SQL has been executed by this change.

## Existing Tables Reused

- `workflows`: workflow definitions, workflow metadata, status, phase, logs, artifact JSON index.
- `runs`: run records, run status, run logs, artifact path.
- `agents`: agent metadata.
- `runners`: external runner state.
- `agent_memory` and `user_memory`: planner memory.
- Validation tables: existing validation instruments, benches, plans, memory, facts, interpretations, and ingestion logs.
- Supabase Storage bucket `artifacts`: artifact source of truth.

## New Tables Required

Run manually only after review:

```sql
supabase/migrations/phase6_api_keys_usage.sql
supabase/migrations/phase6_runs_workflows_patch.sql
supabase/migrations/phase6_plans_credits.sql
```

New additive tables:

- `api_keys`
- `api_usage_events`
- `workflow_events`
- `subscription_plans`
- `user_subscriptions`
- `credit_ledger`

## Required Environment Variables

Existing backend variables remain unchanged:

- `SUPABASE_URL` or `NEXT_PUBLIC_SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ARTIFACT_BUCKET_NAME` optional, defaults to `artifacts`

Development-only local key store:

- `CHIPLOOP_LOCAL_API_KEY_STORE`

Optional entitlement override:

- `CHIPLOOP_AGENT_FACTORY_WRITE_ENABLED=true`

## Manual Supabase SQL Instructions

1. Review each SQL file.
2. Run `phase6_api_keys_usage.sql` first.
3. Run `phase6_runs_workflows_patch.sql` if structured workflow events and metadata columns are desired.
4. Run `phase6_plans_credits.sql` if preparing for billing/credits.
5. Do not drop or rename existing tables.

## Validation After SQL

Create a local or production test key through an approved admin path or local helper:

```powershell
python -m auth_api_keys.create_test_key --user-id dev-user --name local-test --store .chiploop/local_api_keys.json
```

For Supabase-backed validation, confirm:

- `api_keys` stores only `key_hash`, not raw keys.
- `/sdk/agents` rejects missing or invalid keys.
- `/sdk/agents` accepts a valid key.
- `api_usage_events` receives an `sdk_agents_list` event.
- Existing `/run_workflow` behavior is unchanged.

Run tests locally:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_api_keys_usage.py
```

Full current suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py tests/test_studio_contract_registry.py tests/test_studio_planner_factory.py tests/test_chiploop_sdk_cli.py tests/test_api_keys_usage.py tests/test_workflow_dag.py
```

## Rollback Notes

The SQL is additive. If a migration must be backed out, disable SDK/API-key enforcement in application routing first, then archive or ignore the new tables. Do not drop tables until confirming no production events or keys rely on them.

## Risks

- Supabase environments may not have `gen_random_uuid()` enabled. If unavailable, enable `pgcrypto` or replace defaults with the environment's UUID function.
- `workflow_events` is optional and not yet wired into default workflow execution.
- Plan and credit tables are future-facing and not yet connected to Stripe or quota enforcement.
