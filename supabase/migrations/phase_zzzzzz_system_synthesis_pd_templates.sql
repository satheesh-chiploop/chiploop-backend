-- Add System Synthesis and align System PD with the learned Arch2Synthesis/Arch2Tapeout signoff chain.
-- Avoid ON CONFLICT because older environments may not have a unique constraint on workflow template names.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template_agents(name, loop_type, agents) as (
  values
    ('System_Synthesis', 'system', array[
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent'
    ]),
    ('System_PD', 'system', array[
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
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
    ])
),
template_defs as (
  select
    name,
    loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agentNode',
            'position', jsonb_build_object('x', 80 + ((ord - 1) % 4) * 372, 'y', 160 + floor((ord - 1) / 4) * 210),
            'data', jsonb_build_object('uiLabel', agent, 'backendLabel', agent)
          )
          order by ord
        )
        from unnest(agents) with ordinality as item(agent, ord)
      ),
      'edges',
      (
        select coalesce(jsonb_agg(
          jsonb_build_object('id', 'e' || (ord - 1), 'source', 'n' || (ord - 1), 'target', 'n' || ord)
          order by ord
        ), '[]'::jsonb)
        from generate_series(2, array_length(agents, 1)) as item(ord)
      )
    ) as definitions
  from template_agents
)
insert into public.workflows (
  id,
  user_id,
  name,
  loop_type,
  definitions,
  status,
  is_prebuilt,
  created_at,
  updated_at
)
select
  gen_random_uuid(),
  null,
  template_defs.name,
  template_defs.loop_type,
  template_defs.definitions,
  'saved',
  true,
  now(),
  now()
from template_defs
where not exists (
  select 1
  from public.workflows existing
  where existing.name = template_defs.name
    and existing.user_id is null
);

with template_agents(name, loop_type, agents) as (
  values
    ('System_Synthesis', 'system', array[
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent'
    ]),
    ('System_PD', 'system', array[
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
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
    ])
),
template_defs as (
  select
    name,
    loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agentNode',
            'position', jsonb_build_object('x', 80 + ((ord - 1) % 4) * 372, 'y', 160 + floor((ord - 1) / 4) * 210),
            'data', jsonb_build_object('uiLabel', agent, 'backendLabel', agent)
          )
          order by ord
        )
        from unnest(agents) with ordinality as item(agent, ord)
      ),
      'edges',
      (
        select coalesce(jsonb_agg(
          jsonb_build_object('id', 'e' || (ord - 1), 'source', 'n' || (ord - 1), 'target', 'n' || ord)
          order by ord
        ), '[]'::jsonb)
        from generate_series(2, array_length(agents, 1)) as item(ord)
      )
    ) as definitions
  from template_agents
)
update public.workflows as workflow
set loop_type = template_defs.loop_type,
    definitions = template_defs.definitions,
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
from template_defs
where workflow.name = template_defs.name
  and workflow.user_id is null;

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
