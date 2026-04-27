# Phase 6 Schema Reuse Report

Phase 6 inspected current backend Supabase usage and avoids creating duplicate workflow, run, agent, artifact, or validation tables.

No Supabase SQL was executed. The SQL files under `supabase/migrations/` are manual migration scripts only.

## Existing Backend Usage

Current code already reads/writes these core tables:

- `workflows`: workflow definitions, app run metadata, status, phase, logs, loop type, definitions, nodes, edges, artifact JSON index.
- `runs`: per-run status, logs, loop type, artifact path, timestamps.
- `agents`: custom/generated agent metadata and marketplace agent records.
- `runners`: external simulation runner registration, heartbeat, assignment, and status.
- `agent_memory`: agent memory/capability metadata and semantic reuse memory.
- `user_memory`: user planner memory.
- `marketplace_submissions`: workflow/agent publication submissions.
- `notion_user_map`: Notion page mapping.
- Validation tables: `validation_instruments`, `validation_benches`, `validation_bench_connections`, `validation_bench_instruments`, `validation_bench_capabilities`, `validation_test_plans`, `validation_memory`, `validation_run_facts`, `validation_run_interpretations`, `validation_memory_ingestion_log`.
- `spec_coverage` and `design_intent_drafts`.
- Supabase Storage bucket: `artifacts`.

## Phase 6 Need Mapping

| Need | Reuse Existing? | Decision |
| --- | --- | --- |
| `workflow_runs` | Yes | Reuse `runs`. Do not create `workflow_runs`. |
| `workflow_events` | Partial | Existing `workflows.logs` and `runs.logs` remain primary. Add optional `workflow_events` for future structured events. |
| Agent metadata | Yes | Reuse `agents` plus Studio registry YAML metadata. |
| Run status | Yes | Reuse `runs.status`, `workflows.status`, and `workflows.phase`. |
| Artifact metadata | Yes | Reuse Supabase Storage plus `workflows.artifacts` best-effort JSON index. Do not add artifact table in Phase 6. |
| API keys | No | Add `api_keys`; Phase 4A references it, and no existing table is visible for hashed API keys. |
| API usage events | No | Add `api_usage_events`; no existing SDK usage event table is visible. |
| Plans | No | Add `subscription_plans` for future entitlements; not used by current runtime. |
| Subscriptions | No | Add `user_subscriptions` for future billing state; no Stripe coupling. |
| Credit ledger | No | Add `credit_ledger` append-only future usage accounting table. |

## Migration Files

- `supabase/migrations/phase6_api_keys_usage.sql`
  - Creates `api_keys`
  - Creates `api_usage_events`
  - Adds indexes

- `supabase/migrations/phase6_runs_workflows_patch.sql`
  - Creates optional `workflow_events`
  - Adds nullable/default `metadata jsonb` columns to `runs` and `workflows` if missing
  - Does not replace existing logs

- `supabase/migrations/phase6_plans_credits.sql`
  - Creates `subscription_plans`
  - Creates `user_subscriptions`
  - Creates `credit_ledger`
  - Adds indexes

## Backend Repository Layer

Phase 6 adds repository boundaries in `auth_api_keys/repositories.py`:

- `APIKeyRepository`
- `UsageRepository`
- `PlanRepository`
- `SupabaseAPIKeyRepository`
- `SupabaseUsageRepository`

`auth_api_keys.service` now uses these repositories for Supabase-backed API key and usage persistence while keeping the local JSON fallback for development.

## Non-Goals

- No direct Supabase modification.
- No duplicate workflow/run table.
- No artifact metadata table.
- No frontend change.
- No Stripe integration.
- No destructive SQL.
