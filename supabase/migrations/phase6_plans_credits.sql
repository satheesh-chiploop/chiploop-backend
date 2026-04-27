-- Phase 6: future plan, subscription, and credit ledger persistence.
-- Backend enforcement remains a stub until billing is explicitly enabled.
-- Production-safe: creates missing tables/indexes only. No destructive changes.

create table if not exists public.subscription_plans (
  id text primary key,
  name text not null,
  sdk_cli_enabled boolean not null default true,
  monthly_credit_limit integer not null default 1000000,
  agent_factory_write_enabled boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.subscription_plans is
  'Plan definitions for future SDK/API entitlement and billing enforcement.';

create table if not exists public.user_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  plan_id text not null,
  status text not null default 'active',
  current_period_start timestamptz,
  current_period_end timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.user_subscriptions is
  'User subscription state for future billing integration. No Stripe coupling in Phase 6.';

create index if not exists idx_user_subscriptions_user_status
  on public.user_subscriptions (user_id, status);

create table if not exists public.credit_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  workflow_id text,
  api_key_id text,
  event_type text not null,
  credits_delta integer not null,
  balance_after integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

comment on table public.credit_ledger is
  'Append-only credit ledger for future usage accounting.';

create index if not exists idx_credit_ledger_user_created
  on public.credit_ledger (user_id, created_at desc);

create index if not exists idx_credit_ledger_workflow_id
  on public.credit_ledger (workflow_id)
  where workflow_id is not null;

create index if not exists idx_credit_ledger_api_key_id
  on public.credit_ledger (api_key_id)
  where api_key_id is not null;
