-- Product run orchestration: parent run plus per-stage workflow links.

create table if not exists public.product_runs (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references public.products(id) on delete cascade,
  user_id uuid null,
  status text not null default 'queued',
  current_stage text null,
  stage_results jsonb not null default '{}'::jsonb,
  error text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz null
);

create index if not exists idx_product_runs_product_updated
  on public.product_runs (product_id, updated_at desc);

create table if not exists public.product_stage_runs (
  id uuid primary key default gen_random_uuid(),
  product_run_id uuid not null references public.product_runs(id) on delete cascade,
  product_id uuid not null references public.products(id) on delete cascade,
  user_id uuid null,
  stage_id text not null,
  stage_label text not null default '',
  app text not null default '',
  status text not null default 'queued',
  workflow_id uuid null,
  run_id uuid null,
  inputs jsonb not null default '{}'::jsonb,
  outputs jsonb not null default '{}'::jsonb,
  error text null,
  started_at timestamptz null,
  completed_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_product_stage_runs_product_run
  on public.product_stage_runs (product_run_id, created_at);

alter table public.product_runs enable row level security;
alter table public.product_stage_runs enable row level security;

drop policy if exists "Users can read own product runs" on public.product_runs;
create policy "Users can read own product runs"
  on public.product_runs
  for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert own product runs" on public.product_runs;
create policy "Users can insert own product runs"
  on public.product_runs
  for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update own product runs" on public.product_runs;
create policy "Users can update own product runs"
  on public.product_runs
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users can read own product stage runs" on public.product_stage_runs;
create policy "Users can read own product stage runs"
  on public.product_stage_runs
  for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert own product stage runs" on public.product_stage_runs;
create policy "Users can insert own product stage runs"
  on public.product_stage_runs
  for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update own product stage runs" on public.product_stage_runs;
create policy "Users can update own product stage runs"
  on public.product_stage_runs
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
