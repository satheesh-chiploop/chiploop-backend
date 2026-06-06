-- Add post-tapeout LEC to the shared Arch2Tapeout prebuilt workflow.
-- This keeps synthesis LEC and tapeout LEC distinct:
--   - Digital Logic Equivalence Check Agent: RTL vs synthesis netlist
--   - Digital Tapeout Logic Equivalence Check Agent: RTL vs final implementation/tapeout netlist

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template(name, loop_type, agents) as (
  values (
    'Digital_Arch2Tapeout',
    'digital',
    array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital Clock & Reset Architecture Agent',
      'Digital Power Intent (UPF-lite) Agent',
      'Digital RTL Agent',
      'Digital IP Packaging & Handoff Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital UPF Static Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent',
      'Digital STA PrePlace Agent',
      'Digital Floorplan Agent',
      'Digital Placement Agent',
      'Digital STA PostPlace Agent',
      'Digital CTS Agent',
      'Digital STA PostCTS Agent',
      'Digital Route Agent',
      'Digital STA PostRoute Agent',
      'Digital Fill Agent',
      'Digital DRC Agent',
      'Digital LVS Agent',
      'Digital Tapeout Agent',
      'Digital Tapeout Logic Equivalence Check Agent',
      'Digital Executive Summary Agent'
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
              'x', 80 + (((ord - 1) % 5) * 340),
              'y', 160 + (((ord - 1) / 5) * 190)
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
