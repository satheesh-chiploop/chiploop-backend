-- Add the bounded Verify Closure Loop prebuilt workflow template.
-- This is additive and keeps the existing recommendation-only Digital_Verify_Closure template.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template(name, loop_type, agents) as (
  values (
    'Digital_Verify_Closure_Loop',
    'digital',
    array[
      'Digital Verify Closure Ingest Agent',
      'Digital Coverage Gap Analysis Agent',
      'Digital Failure Triage Agent',
      'Digital Closure Recommendation Agent',
      'Digital Verification Plan Update Agent',
      'Digital Coverage Plan Update Agent',
      'Digital Testcase Seed Update Agent',
      'Digital Closure Rerun Planner Agent',
      'Digital Verification Handoff Ingest Agent',
      'Digital Testbench Generator Agent',
      'Digital Assertions (SVA) Agent',
      'Digital Functional Coverage Agent',
      'Digital Simulation Control Agent',
      'Digital Simulation Execution Agent',
      'Digital Simulation Summary Coverage Agent',
      'Digital Closure Iteration Judge Agent'
    ]::text[]
  )
),
definitions as (
  select
    t.name,
    t.loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agentNode',
            'position', jsonb_build_object(
              'x', 80 + (((ord - 1) % 4) * 372),
              'y', 160 + (((ord - 1) / 4) * 210)
            ),
            'data', jsonb_build_object(
              'desc', 'Auto-composed: ' || agent_name,
              'uiLabel', agent_name,
              'backendLabel', agent_name
            )
          )
          order by ord
        )
        from unnest(t.agents) with ordinality as a(agent_name, ord)
      ),
      'edges',
      coalesce(
        (
          select jsonb_agg(
            jsonb_build_object(
              'id', 'e' || ord,
              'source', 'n' || ord,
              'target', 'n' || (ord + 1)
            )
            order by ord
          )
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      )
    ) as definitions
  from template t
),
updated as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = d.loop_type,
      is_prebuilt = true,
      user_id = null,
      status = 'saved',
      updated_at = now()
  from definitions d
  where w.name = d.name
    and w.user_id is null
  returning w.name
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(),
  null,
  d.name,
  d.loop_type,
  d.definitions,
  d.definitions->'nodes',
  d.definitions->'edges',
  'saved',
  true,
  now(),
  now()
from definitions d
where not exists (
  select 1
  from public.workflows w
  where w.name = d.name
    and w.user_id is null
);

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
