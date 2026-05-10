create table if not exists public.github_installations (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  github_installation_id text not null,
  github_account_login text,
  github_account_type text,
  permissions jsonb not null default '{}'::jsonb,
  repository_selection text,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, github_installation_id)
);

create index if not exists github_installations_user_id_idx
  on public.github_installations(user_id);

comment on table public.github_installations is
  'Per-user GitHub App installations authorized for ChipLoop imports/exports.';
