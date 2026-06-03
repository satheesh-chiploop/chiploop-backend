-- Hybrid Private Backend AI billing and model usage foundation.
-- Production-safe: creates missing tables/indexes only. No destructive changes.

create table if not exists public.customer_accounts (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug text unique,
  status text not null default 'active',
  deployment_mode text not null default 'hybrid_private_backend',
  ai_billing_mode text not null default 'byok',
  model_key_owner text not null default 'customer',
  monthly_ai_credit_limit integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint customer_accounts_ai_billing_mode_check
    check (ai_billing_mode in ('byok', 'chiploop_managed')),
  constraint customer_accounts_model_key_owner_check
    check (model_key_owner in ('customer', 'chiploop'))
);

comment on table public.customer_accounts is
  'Organization/customer billing and deployment settings for hosted SaaS, Hybrid Private Backend, and private deployments.';

create table if not exists public.customer_deployments (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid references public.customer_accounts(id) on delete cascade,
  deployment_name text not null,
  deployment_mode text not null default 'hybrid_private_backend',
  frontend_url text,
  backend_url text,
  supabase_project_ref text,
  license_mode text not null default 'hybrid_private_backend',
  license_status text not null default 'active',
  status text not null default 'provisioning',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.customer_deployments is
  'Customer deployment records for Hybrid Private Backend, ChipLoop Private, and customer cloud installs.';

create index if not exists idx_customer_deployments_customer_id
  on public.customer_deployments(customer_id);

create table if not exists public.model_usage_events (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid references public.customer_accounts(id) on delete set null,
  org_id text,
  user_id text,
  workflow_id text,
  run_id text,
  agent_name text,
  capability text,
  provider text not null,
  model text not null,
  ai_billing_mode text not null default 'byok',
  model_key_owner text not null default 'customer',
  request_type text not null default 'chat',
  stream boolean not null default false,
  input_tokens integer,
  output_tokens integer,
  total_tokens integer,
  input_chars integer,
  output_chars integer,
  estimated_cost_usd numeric(12, 6),
  estimated_credits integer,
  latency_ms integer,
  status text not null default 'ok',
  error_type text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint model_usage_ai_billing_mode_check
    check (ai_billing_mode in ('byok', 'chiploop_managed')),
  constraint model_usage_model_key_owner_check
    check (model_key_owner in ('customer', 'chiploop'))
);

comment on table public.model_usage_events is
  'Append-only per-model-call usage events for model cost, token, and AI-credit accounting.';

create index if not exists idx_model_usage_events_user_created
  on public.model_usage_events(user_id, created_at desc)
  where user_id is not null;

create index if not exists idx_model_usage_events_org_created
  on public.model_usage_events(org_id, created_at desc)
  where org_id is not null;

create index if not exists idx_model_usage_events_customer_created
  on public.model_usage_events(customer_id, created_at desc)
  where customer_id is not null;

create index if not exists idx_model_usage_events_workflow
  on public.model_usage_events(workflow_id, run_id)
  where workflow_id is not null;

create index if not exists idx_model_usage_events_billing_period
  on public.model_usage_events(ai_billing_mode, created_at desc);

create table if not exists public.org_credit_ledger (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid references public.customer_accounts(id) on delete set null,
  org_id text,
  user_id text,
  workflow_id text,
  run_id text,
  model_usage_event_id uuid references public.model_usage_events(id) on delete set null,
  event_type text not null,
  credits_delta integer not null,
  balance_after integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

comment on table public.org_credit_ledger is
  'Append-only organization/customer credit ledger for pooled Enterprise and Hybrid Private Backend AI credits.';

create index if not exists idx_org_credit_ledger_org_created
  on public.org_credit_ledger(org_id, created_at desc)
  where org_id is not null;

create index if not exists idx_org_credit_ledger_customer_created
  on public.org_credit_ledger(customer_id, created_at desc)
  where customer_id is not null;

create index if not exists idx_org_credit_ledger_user_created
  on public.org_credit_ledger(user_id, created_at desc)
  where user_id is not null;
