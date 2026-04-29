-- Phase 7H/7I private agents + marketplace foundation.
-- Safe to review and run manually in Supabase. This migration is non-destructive:
-- it only adds missing columns, comments, and indexes.

alter table public.agents
  add column if not exists owner_id uuid,
  add column if not exists agent_spec jsonb,
  add column if not exists skills jsonb,
  add column if not exists tools jsonb,
  add column if not exists hooks jsonb,
  add column if not exists generated_files jsonb,
  add column if not exists registry_patch jsonb,
  add column if not exists status text default 'private',
  add column if not exists visibility text default 'private',
  add column if not exists source text default 'studio_factory',
  add column if not exists submitted_at timestamptz,
  add column if not exists reviewed_at timestamptz,
  add column if not exists reviewed_by uuid,
  add column if not exists review_notes text;

comment on column public.agents.owner_id is
  'Supabase auth user id that owns a private/custom agent. Null means platform/global where applicable.';
comment on column public.agents.agent_spec is
  'Studio Contract AgentSpec proposed or approved for this agent.';
comment on column public.agents.skills is
  'Studio Contract skill references or proposed skill specs for this private agent.';
comment on column public.agents.tools is
  'Studio Contract tool/plugin references for this private agent.';
comment on column public.agents.hooks is
  'Studio Contract lifecycle hook references for this private agent.';
comment on column public.agents.generated_files is
  'Reviewable Agent Factory generated file plans. Do not treat as production code until approved.';
comment on column public.agents.registry_patch is
  'Reviewable registry patch proposal for future manual/global promotion.';
comment on column public.agents.status is
  'Private agent lifecycle: draft, private, submitted, approved, rejected.';
comment on column public.agents.visibility is
  'Visibility: private, global, marketplace, marketplace_pending. Submit must not make an agent global.';
comment on column public.agents.source is
  'Creation source such as studio_factory, legacy_generate_missing_agents, or manual.';

create index if not exists idx_agents_owner_id
  on public.agents(owner_id);

create index if not exists idx_agents_visibility
  on public.agents(visibility);

create index if not exists idx_agents_status
  on public.agents(status);

create index if not exists idx_agents_loop_type
  on public.agents(loop_type);

create index if not exists idx_agents_owner_visibility_status
  on public.agents(owner_id, visibility, status);

-- RLS policy recommendations for manual review before enabling:
-- 1. Users can read their own private agents:
--    using (auth.uid() = owner_id and visibility = 'private')
-- 2. Users can insert private custom agents for themselves:
--    with check (auth.uid() = owner_id and visibility = 'private' and is_custom = true)
-- 3. Users can update/delete their own private non-approved agents:
--    using (auth.uid() = owner_id and visibility = 'private')
-- 4. Authenticated users can read approved global/marketplace agents:
--    using (visibility in ('global', 'marketplace') or status = 'approved' or is_prebuilt = true)
-- 5. Service role/admin handles review approval/rejection and global promotion.
--
-- This migration does not enable RLS or create policies because the existing
-- application currently uses a service role backend and may already have table
-- policies. Apply RLS changes separately after reviewing current Supabase policy state.
