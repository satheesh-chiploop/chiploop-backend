-- Ensure the shared Digital_Smoke prebuilt template runs real simulation.
-- Older deployed databases may already have phase_zz_digital_prebuilt_chain_alignment.sql
-- applied, so this late migration updates Digital_Smoke idempotently.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template(name, agents) as (
  values
    ('Digital_Smoke', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Smoke Preflight Agent',
      'Digital RTL Agent',
      'Digital Testbench Generator Agent',
      'Digital Assertions (SVA) Agent',
      'Digital Simulation Control Agent',
      'Digital Simulation Execution Agent',
      'Digital Simulation Summary Coverage Agent',
      'Digital Bug Localization Agent',
      'Digital Smoke Executive Summary Agent'
    ]::text[])
),
definitions as (
  select
    t.name,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agent',
            'position', jsonb_build_object('x', 80 + ((ord - 1) * 240), 'y', 180),
            'data', jsonb_build_object(
              'uiLabel', replace(agent_name, 'Digital ', ''),
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
            jsonb_build_object('id', 'e' || ord, 'source', 'n' || ord, 'target', 'n' || (ord + 1))
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
      loop_type = 'digital',
      is_prebuilt = true,
      user_id = null,
      status = coalesce(w.status, 'saved'),
      updated_at = now()
  from definitions d
  where w.name = d.name
    and coalesce(w.is_prebuilt, false) = true
  returning w.name
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(),
  null,
  d.name,
  'digital',
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
    and coalesce(w.is_prebuilt, false) = true
);
