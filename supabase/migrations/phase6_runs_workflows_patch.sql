-- Phase 6: optional normalized workflow event log.
-- Existing workflow/run status continues to use public.workflows and public.runs.
-- This table is additive and does not replace workflows.logs or runs.logs.

create table if not exists public.workflow_events (
  id uuid primary key default gen_random_uuid(),
  workflow_id text not null,
  run_id text,
  user_id text,
  event_type text not null,
  agent_name text,
  loop_type text,
  status text,
  message text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

comment on table public.workflow_events is
  'Optional structured workflow/run event log. Existing workflows.logs and runs.logs remain supported.';

create index if not exists idx_workflow_events_workflow_created
  on public.workflow_events (workflow_id, created_at desc);

create index if not exists idx_workflow_events_run_created
  on public.workflow_events (run_id, created_at desc)
  where run_id is not null;

create index if not exists idx_workflow_events_user_created
  on public.workflow_events (user_id, created_at desc)
  where user_id is not null;

create index if not exists idx_workflow_events_type_created
  on public.workflow_events (event_type, created_at desc);

-- Add only broadly useful metadata columns when missing.
-- These are nullable and do not alter existing workflow behavior.
alter table if exists public.runs
  add column if not exists metadata jsonb not null default '{}'::jsonb;

alter table if exists public.workflows
  add column if not exists metadata jsonb not null default '{}'::jsonb;
