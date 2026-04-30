-- Agent marketplace ecosystem foundation.
-- Production-safe: creates missing tables/indexes only. No destructive changes.

create table if not exists public.marketplace_agent_listings (
  id uuid primary key default gen_random_uuid(),
  source_agent_id text,
  approved_submission_id text,
  name text not null,
  slug text not null unique,
  description text,
  domain text,
  loop_type text,
  agent_spec jsonb not null default '{}'::jsonb,
  skills jsonb not null default '[]'::jsonb,
  tools jsonb not null default '[]'::jsonb,
  hooks jsonb not null default '[]'::jsonb,
  version text not null default '1.0.0',
  publisher_user_id text,
  visibility text not null default 'public',
  status text not null default 'active',
  install_count integer not null default 0,
  average_rating numeric(3,2) not null default 0,
  review_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_marketplace_listings_status_loop
  on public.marketplace_agent_listings (status, loop_type);

create index if not exists idx_marketplace_listings_domain
  on public.marketplace_agent_listings (domain);

create table if not exists public.marketplace_agent_versions (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references public.marketplace_agent_listings(id) on delete cascade,
  version text not null,
  agent_spec jsonb not null default '{}'::jsonb,
  skills jsonb not null default '[]'::jsonb,
  tools jsonb not null default '[]'::jsonb,
  hooks jsonb not null default '[]'::jsonb,
  generated_files jsonb not null default '[]'::jsonb,
  registry_patch jsonb not null default '{}'::jsonb,
  release_notes text,
  created_at timestamptz not null default now(),
  unique (listing_id, version)
);

create table if not exists public.marketplace_installs (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  listing_id uuid not null references public.marketplace_agent_listings(id) on delete cascade,
  version text not null,
  installed_agent_id text,
  status text not null default 'installed',
  created_at timestamptz not null default now(),
  unique (user_id, listing_id, version)
);

create index if not exists idx_marketplace_installs_user
  on public.marketplace_installs (user_id, created_at desc);

create table if not exists public.marketplace_reviews (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references public.marketplace_agent_listings(id) on delete cascade,
  user_id text not null,
  rating integer not null check (rating between 1 and 5),
  review_text text,
  hidden boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (listing_id, user_id)
);

create index if not exists idx_marketplace_reviews_listing
  on public.marketplace_reviews (listing_id, hidden, created_at desc);

alter table if exists public.marketplace_submissions
  add column if not exists reviewed_at timestamptz,
  add column if not exists reviewed_by text,
  add column if not exists review_notes text,
  add column if not exists approved_listing_id text;

comment on table public.marketplace_agent_listings is
  'Approved marketplace agent listings. Private source agents remain private; listings are separate approved artifacts.';

comment on table public.marketplace_installs is
  'User-scoped installations of marketplace agents. Installs create or reference a private workspace agent.';

-- Recommended RLS shape if enabled:
-- 1. Authenticated users can read active public marketplace_agent_listings.
-- 2. Authenticated users can insert/read their own marketplace_installs.
-- 3. Authenticated users can read non-hidden reviews and upsert their own review.
-- 4. Marketplace admin/service role manages submissions and approval state.
