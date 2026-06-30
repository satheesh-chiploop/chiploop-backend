-- Private Studio apps created from user-owned workflows.
-- Additive only: app drafts remain private until a separate admin-approved
-- marketplace flow promotes them.

create table if not exists public.user_apps (
  id uuid primary key default gen_random_uuid(),
  owner_id text not null,
  workflow_id text not null,
  workflow_name text,
  name text not null,
  slug text not null,
  description text,
  category text,
  loop_type text,
  input_schema jsonb not null default '{}'::jsonb,
  output_schema jsonb not null default '{}'::jsonb,
  default_config jsonb not null default '{}'::jsonb,
  app_config jsonb not null default '{}'::jsonb,
  visibility text not null default 'private',
  status text not null default 'private',
  marketplace_status text not null default 'draft',
  price_usd numeric(10,2),
  submitted_at timestamptz,
  reviewed_at timestamptz,
  reviewed_by text,
  review_notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_id, slug)
);

create index if not exists idx_user_apps_owner_created
  on public.user_apps(owner_id, created_at desc);

create index if not exists idx_user_apps_workflow
  on public.user_apps(workflow_id);

create index if not exists idx_user_apps_marketplace_status
  on public.user_apps(marketplace_status, status);

comment on table public.user_apps is
  'User-owned private app drafts packaged from Studio workflows. Marketplace approval should publish a separate approved listing.';

comment on column public.user_apps.app_config is
  'App runtime configuration and workflow snapshot used to render or run the app wrapper.';

comment on column public.user_apps.marketplace_status is
  'Draft/review lifecycle for marketplace submission: draft, pending, approved, rejected.';

create table if not exists public.marketplace_app_listings (
  id uuid primary key default gen_random_uuid(),
  source_app_id text,
  source_workflow_id text,
  approved_submission_id text,
  name text not null,
  slug text not null unique,
  description text,
  category text,
  loop_type text,
  workflow_id text,
  workflow_name text,
  input_schema jsonb not null default '{}'::jsonb,
  output_schema jsonb not null default '{}'::jsonb,
  default_config jsonb not null default '{}'::jsonb,
  app_config jsonb not null default '{}'::jsonb,
  workflow_snapshot jsonb not null default '{}'::jsonb,
  price_usd numeric(10,2),
  publisher_user_id text,
  visibility text not null default 'public',
  status text not null default 'active',
  install_count integer not null default 0,
  average_rating numeric(3,2) not null default 0,
  review_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_marketplace_app_listings_status_loop
  on public.marketplace_app_listings(status, loop_type);

create index if not exists idx_marketplace_app_listings_category
  on public.marketplace_app_listings(category);

create table if not exists public.marketplace_app_versions (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references public.marketplace_app_listings(id) on delete cascade,
  version text not null,
  input_schema jsonb not null default '{}'::jsonb,
  output_schema jsonb not null default '{}'::jsonb,
  default_config jsonb not null default '{}'::jsonb,
  app_config jsonb not null default '{}'::jsonb,
  workflow_snapshot jsonb not null default '{}'::jsonb,
  release_notes text,
  created_at timestamptz not null default now(),
  unique (listing_id, version)
);

create table if not exists public.marketplace_app_installs (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  listing_id uuid not null references public.marketplace_app_listings(id) on delete cascade,
  installed_user_app_id text,
  status text not null default 'installed',
  created_at timestamptz not null default now()
);

create index if not exists idx_marketplace_app_installs_user
  on public.marketplace_app_installs(user_id, created_at desc);

comment on table public.marketplace_app_listings is
  'Approved public marketplace app listings created from reviewed private user_apps.';

comment on table public.marketplace_app_installs is
  'User-scoped marketplace app install audit. Runtime install creates a private user_apps copy.';

-- Recommended RLS shape if enabled:
-- 1. Users can read/insert/update/delete their own private app drafts.
-- 2. Authenticated users can read approved public marketplace listings, not raw private user_apps.
-- 3. Service role/admin handles review approval and marketplace publication.
