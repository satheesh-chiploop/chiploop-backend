-- Phase 6: API keys and SDK usage events.
-- Production-safe: creates missing tables/indexes only. No destructive changes.

create table if not exists public.api_keys (
  id text primary key,
  key_hash text not null unique,
  key_prefix text not null,
  user_id text not null,
  name text not null,
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  last_used_at timestamptz
);

comment on table public.api_keys is
  'Stores hashed ChipLoop SDK/API keys. Raw keys are never stored.';
comment on column public.api_keys.key_hash is
  'SHA-256 hash of cl_live_* or cl_test_* key.';
comment on column public.api_keys.key_prefix is
  'Short display prefix for user-facing key identification.';

create index if not exists idx_api_keys_user_id
  on public.api_keys (user_id);

create index if not exists idx_api_keys_key_hash_active
  on public.api_keys (key_hash)
  where revoked_at is null;

create table if not exists public.api_usage_events (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  api_key_id text not null,
  endpoint text not null,
  workflow_id text,
  event_type text not null,
  created_at timestamptz not null default now()
);

comment on table public.api_usage_events is
  'Append-only SDK/API usage events for Phase 4A/Phase 6 metering.';

create index if not exists idx_api_usage_events_user_created
  on public.api_usage_events (user_id, created_at desc);

create index if not exists idx_api_usage_events_api_key_created
  on public.api_usage_events (api_key_id, created_at desc);

create index if not exists idx_api_usage_events_workflow_id
  on public.api_usage_events (workflow_id)
  where workflow_id is not null;
