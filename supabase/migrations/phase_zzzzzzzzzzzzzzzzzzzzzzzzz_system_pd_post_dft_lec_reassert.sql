-- Reassert System_PD source-of-truth workflow with Post-DFT LEC.
-- Avoid ON CONFLICT because some deployed projects do not have a unique
-- constraint on workflow name.

alter table if exists public.workflows
  add column if not exists definitions jsonb,
  add column if not exists nodes jsonb,
  add column if not exists edges jsonb,
  add column if not exists loop_type text,
  add column if not exists status text,
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists created_at timestamptz default now(),
  add column if not exists updated_at timestamptz default now();

with workflow_template as (
  select 'System_PD'::text as name, 'system'::text as loop_type, array[
    'Digital Spec Agent',
    'Digital Architecture Agent',
    'Digital Microarchitecture Agent',
    'Digital Register Map Agent',
    'Digital RTL Agent',
    'Digital RTL Linting Agent',
    'Digital RTL Signature Agent',
    'Analog Spec Builder Agent',
    'Analog Behavioral Model Agent',
    'Analog Abstract Views Agent',
    'System Integration Intent Agent',
    'System Top Assembly Agent',
    'Analog Macro Interface Contract Agent',
    'System Assertions (SVA) Agent',
    'System RTL Handoff Package Agent',
    'System RTL Evidence Dashboard Agent',
    'Analog Macro Interface Validation Agent',
    'Digital Foundry Profile Agent',
    'Digital Implementation Setup Agent',
    'Digital Synthesis Agent',
    'Digital Logic Equivalence Check Agent',
    'Digital DFT Scan Stitching Agent',
    'Digital Post-DFT Logic Equivalence Check Agent',
    'System Synthesis Closure Agent',
    'Digital Scan ATPG Coverage Agent',
    'Digital MBIST Collateral Agent',
    'Analog Sky130 SPICE Netlist Agent',
    'Analog GDS Generation Agent',
    'Analog LEF Extraction Agent',
    'Analog Liberty Characterization Agent',
    'Analog Collateral Consistency Agent',
    'Analog Physical Collateral Package Agent',
    'Digital STA PrePlace Agent',
    'Digital Floorplan Agent',
    'Digital Placement Agent',
    'Digital STA PostPlace Agent',
    'Digital CTS Agent',
    'Digital STA PostCTS Agent',
    'Digital Route Agent',
    'Digital STA PostRoute Agent',
    'Digital Fill Agent',
    'Digital STA PostFill Agent',
    'Digital DRC Agent',
    'Digital LVS Agent',
    'Digital Tapeout Agent',
    'Digital Tapeout Logic Equivalence Check Agent',
    'Digital Executive Summary Agent',
    'System PD Signoff Closure Agent'
  ]::text[] as agents
),
expanded as (
  select
    wt.name,
    wt.loop_type,
    jsonb_agg(
      jsonb_build_object(
        'id', 'n' || ord::text,
        'type', 'agentNode',
        'position', jsonb_build_object('x', 80 + (((ord - 1) % 4) * 372), 'y', 160 + (((ord - 1) / 4) * 210)),
        'data', jsonb_build_object('desc', 'Auto-composed: ' || agent, 'uiLabel', agent, 'backendLabel', agent)
      )
      order by ord
    ) as nodes,
    jsonb_agg(
      jsonb_build_object('id', 'e' || (ord - 1)::text, 'source', 'n' || (ord - 1)::text, 'target', 'n' || ord::text)
      order by ord
    ) filter (where ord > 1) as edges
  from workflow_template wt
  cross join lateral unnest(wt.agents) with ordinality as a(agent, ord)
  group by wt.name, wt.loop_type
),
updated as (
  update public.workflows workflow
  set
    loop_type = expanded.loop_type,
    definitions = jsonb_build_object('nodes', expanded.nodes, 'edges', coalesce(expanded.edges, '[]'::jsonb)),
    nodes = expanded.nodes,
    edges = coalesce(expanded.edges, '[]'::jsonb),
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
  from expanded
  where workflow.name = expanded.name
  returning workflow.id
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(),
  null,
  expanded.name,
  expanded.loop_type,
  jsonb_build_object('nodes', expanded.nodes, 'edges', coalesce(expanded.edges, '[]'::jsonb)),
  expanded.nodes,
  coalesce(expanded.edges, '[]'::jsonb),
  'saved',
  true,
  now(),
  now()
from expanded
where not exists (select 1 from updated)
  and not exists (
    select 1 from public.workflows workflow
    where workflow.name = expanded.name
  );
