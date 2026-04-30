-- First-run guided onboarding state for the Apps home Arch2RTL demo.
-- Production-safe: creates one small user-scoped table only. No destructive changes.

create table if not exists public.user_onboarding (
  user_id text primary key,
  completed_at timestamptz,
  skipped_at timestamptz,
  demo_started_at timestamptz,
  first_workflow_id text,
  last_step text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.user_onboarding is
  'User-scoped first-run onboarding state. Used to show the guided Arch2RTL demo once and allow users to skip or complete it.';

comment on column public.user_onboarding.first_workflow_id is
  'Optional workflow id for the users first completed onboarding workflow.';

create index if not exists idx_user_onboarding_completed_at
  on public.user_onboarding (completed_at)
  where completed_at is not null;

create index if not exists idx_user_onboarding_demo_started_at
  on public.user_onboarding (demo_started_at desc)
  where demo_started_at is not null;

-- Recommended RLS policy shape if RLS is enabled manually:
-- alter table public.user_onboarding enable row level security;
-- create policy "Users can read their onboarding"
--   on public.user_onboarding for select
--   using (auth.uid()::text = user_id);
-- create policy "Users can insert their onboarding"
--   on public.user_onboarding for insert
--   with check (auth.uid()::text = user_id);
-- create policy "Users can update their onboarding"
--   on public.user_onboarding for update
--   using (auth.uid()::text = user_id)
--   with check (auth.uid()::text = user_id);
