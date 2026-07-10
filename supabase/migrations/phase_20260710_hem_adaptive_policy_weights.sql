-- HEM Adaptive Policy Weights.
-- Stores per-user workflow-stage transition weights for Hebbian-style policy learning.

create table if not exists public.hem_policy_weights (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  policy_key text not null default 'digital_rtl_default',
  from_stage text not null,
  to_stage text not null,
  weight double precision not null default 1.0,
  learning_rate double precision not null default 0.1,
  attempt_count integer not null default 0,
  success_count integer not null default 0,
  last_outcome text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, policy_key, from_stage, to_stage)
);

create index if not exists idx_hem_policy_weights_user_policy
  on public.hem_policy_weights (user_id, policy_key, from_stage, weight desc);

create index if not exists idx_hem_policy_weights_updated
  on public.hem_policy_weights (updated_at desc);
