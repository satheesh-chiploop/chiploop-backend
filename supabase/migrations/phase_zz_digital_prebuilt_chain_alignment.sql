-- Align digital prebuilt workflows with current chained-handoff behavior.
-- This is intentionally late alphabetically so it is the final template update
-- after older prebuilt workflow migrations.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with templates(name, agents) as (
  values
    ('Digital_DQA', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital RTL Linting Agent',
      'Digital CDC Analysis Agent',
      'Digital Reset Integrity Agent',
      'Digital Synthesis Readiness Agent',
      'Digital Executive Summary Agent'
    ]::text[]),
    ('Digital_Verify', array[
      'Digital Verification Handoff Ingest Agent',
      'Digital Testbench Generator Agent',
      'Digital Assertions (SVA) Agent',
      'Digital Functional Coverage Agent',
      'Digital Simulation Control Agent',
      'Digital Simulation Execution Agent',
      'Digital Simulation Summary Coverage Agent'
    ]::text[]),
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
    ]::text[]),
    ('Digital_Integrate', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital RTL Signature Agent',
      'Digital Integration Intent Agent',
      'Digital Top Assembly Agent',
      'Digital IP Packaging & Handoff Agent'
    ]::text[]),
    ('Digital_Arch2Synthesis', array[
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
      'Digital Synthesis Agent'
    ]::text[]),
    ('Digital_Arch2Tapeout', array[
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
      'Digital Executive Summary Agent'
    ]::text[]),
    ('Digital_Arch2Sim', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital Clock & Reset Architecture Agent',
      'Digital Power Intent (UPF-lite) Agent',
      'Digital RTL Agent',
      'Digital IP Packaging & Handoff Agent',
      'Digital Testbench Generator Agent',
      'Digital Assertions (SVA) Agent',
      'Digital Functional Coverage Agent',
      'Digital Simulation Control Agent',
      'Digital Simulation Execution Agent',
      'Digital Simulation Summary Coverage Agent',
      'Digital Executive Summary Agent'
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
            'type', agent_name,
            'position', jsonb_build_object('x', 100 + ((ord - 1) * 350), 'y', 200),
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
            jsonb_build_object('source', 'n' || ord, 'target', 'n' || (ord + 1))
            order by ord
          )
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      )
    ) as definitions
  from templates t
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
