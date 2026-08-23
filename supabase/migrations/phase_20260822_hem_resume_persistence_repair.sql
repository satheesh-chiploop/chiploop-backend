-- Production repair for HEM resume persistence.
-- Idempotent so environments that already applied the original HEM migration
-- retain their data while partially migrated environments gain missing fields.

create table if not exists public.hem_runs (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  root_workflow_id text not null,
  root_run_id text,
  mode text not null default 'fixed',
  policy_key text not null default 'digital_rtl_default',
  current_workflow_id text,
  current_run_id text,
  current_stage text,
  next_stage text,
  status text not null default 'running',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.hem_runs add column if not exists user_id text;
alter table public.hem_runs add column if not exists root_workflow_id text;
alter table public.hem_runs add column if not exists root_run_id text;
alter table public.hem_runs add column if not exists mode text default 'fixed';
alter table public.hem_runs add column if not exists policy_key text default 'digital_rtl_default';
alter table public.hem_runs add column if not exists current_workflow_id text;
alter table public.hem_runs add column if not exists current_run_id text;
alter table public.hem_runs add column if not exists current_stage text;
alter table public.hem_runs add column if not exists next_stage text;
alter table public.hem_runs add column if not exists status text default 'running';
alter table public.hem_runs add column if not exists metadata jsonb default '{}'::jsonb;
alter table public.hem_runs add column if not exists created_at timestamptz default now();
alter table public.hem_runs add column if not exists updated_at timestamptz default now();

create index if not exists idx_hem_runs_user_created
  on public.hem_runs (user_id, created_at desc);
create index if not exists idx_hem_runs_root_workflow
  on public.hem_runs (root_workflow_id);
create index if not exists idx_hem_runs_current_workflow
  on public.hem_runs (current_workflow_id)
  where current_workflow_id is not null;

create table if not exists public.hem_run_events (
  id uuid primary key default gen_random_uuid(),
  hem_run_id uuid references public.hem_runs(id) on delete cascade,
  user_id text not null,
  workflow_id text,
  run_id text,
  stage text,
  event_type text not null,
  status text,
  message text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_hem_run_events_hem_created
  on public.hem_run_events (hem_run_id, created_at desc);
create index if not exists idx_hem_run_events_user_created
  on public.hem_run_events (user_id, created_at desc);
create index if not exists idx_hem_run_events_workflow
  on public.hem_run_events (workflow_id)
  where workflow_id is not null;

