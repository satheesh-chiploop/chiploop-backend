-- Reassert System DQA source-RTL behavior and analog macro interface contract
-- source-of-truth definitions.

alter table public.workflows
  add column if not exists definitions jsonb,
  add column if not exists nodes jsonb,
  add column if not exists edges jsonb,
  add column if not exists is_prebuilt boolean not null default false;

with template(name, loop_type, agents) as (
  values
    (
      'System_RTL',
      'system',
      array[
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
        'System RTL Evidence Dashboard Agent'
      ]::text[]
    ),
    (
      'System_DQA',
      'system',
      array[
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
        'Digital RTL Linting Agent',
        'Digital CDC Analysis Agent',
        'Digital Reset Integrity Agent',
        'Digital Synthesis Readiness Agent',
        'Digital DQA Summary Agent'
      ]::text[]
    ),
    (
      'System_PD',
      'system',
      array[
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
        'Digital Fill Agent',
        'Digital DRC Agent',
        'Digital LVS Agent',
        'Digital Tapeout Agent',
        'Digital Tapeout Logic Equivalence Check Agent',
        'Digital Executive Summary Agent'
      ]::text[]
    )
),
defs as (
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
            'position', jsonb_build_object(
              'x', 80 + ((ord - 1) % 4) * 372,
              'y', 160 + floor((ord - 1) / 4) * 210
            ),
            'data', jsonb_build_object(
              'desc', 'Auto-composed: ' || agent,
              'uiLabel', agent,
              'backendLabel', agent
            )
          )
          order by ord
        )
        from unnest(agents) with ordinality as u(agent, ord)
      ),
      'edges',
      (
        select coalesce(jsonb_agg(
          jsonb_build_object(
            'id', 'e' || ord,
            'source', 'n' || ord,
            'target', 'n' || (ord + 1)
          )
          order by ord
        ), '[]'::jsonb)
        from generate_series(1, greatest(array_length(agents, 1) - 1, 0)) as ord
      )
    ) as definitions
  from template
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(),
  null,
  name,
  loop_type,
  definitions,
  definitions->'nodes',
  definitions->'edges',
  'saved',
  true,
  now(),
  now()
from defs
where not exists (
  select 1 from public.workflows w
  where w.name = defs.name and coalesce(w.is_prebuilt, false) = true
);

with template(name, loop_type, agents) as (
  values
    ('System_RTL','system', array[
      'Digital Spec Agent','Digital Architecture Agent','Digital Microarchitecture Agent','Digital Register Map Agent','Digital RTL Agent','Digital RTL Linting Agent','Digital RTL Signature Agent','Analog Spec Builder Agent','Analog Behavioral Model Agent','Analog Abstract Views Agent','System Integration Intent Agent','System Top Assembly Agent','Analog Macro Interface Contract Agent','System Assertions (SVA) Agent','System RTL Handoff Package Agent','System RTL Evidence Dashboard Agent'
    ]::text[]),
    ('System_DQA','system', array[
      'Digital Spec Agent','Digital Architecture Agent','Digital Microarchitecture Agent','Digital Register Map Agent','Digital RTL Agent','Digital RTL Linting Agent','Digital RTL Signature Agent','Analog Spec Builder Agent','Analog Behavioral Model Agent','Analog Abstract Views Agent','System Integration Intent Agent','System Top Assembly Agent','Analog Macro Interface Contract Agent','System Assertions (SVA) Agent','System RTL Handoff Package Agent','System RTL Evidence Dashboard Agent','Digital RTL Linting Agent','Digital CDC Analysis Agent','Digital Reset Integrity Agent','Digital Synthesis Readiness Agent','Digital DQA Summary Agent'
    ]::text[]),
    ('System_PD','system', array[
      'Digital Spec Agent','Digital Architecture Agent','Digital Microarchitecture Agent','Digital Register Map Agent','Digital RTL Agent','Digital RTL Linting Agent','Digital RTL Signature Agent','Analog Spec Builder Agent','Analog Behavioral Model Agent','Analog Abstract Views Agent','System Integration Intent Agent','System Top Assembly Agent','Analog Macro Interface Contract Agent','System Assertions (SVA) Agent','System RTL Handoff Package Agent','System RTL Evidence Dashboard Agent','Analog Macro Interface Validation Agent','Digital Foundry Profile Agent','Digital Implementation Setup Agent','Digital Synthesis Agent','Digital Logic Equivalence Check Agent','Digital DFT Scan Stitching Agent','Digital Scan ATPG Coverage Agent','Digital MBIST Collateral Agent','Analog Sky130 SPICE Netlist Agent','Analog GDS Generation Agent','Analog LEF Extraction Agent','Analog Liberty Characterization Agent','Analog Collateral Consistency Agent','Analog Physical Collateral Package Agent','Digital STA PrePlace Agent','Digital Floorplan Agent','Digital Placement Agent','Digital STA PostPlace Agent','Digital CTS Agent','Digital STA PostCTS Agent','Digital Route Agent','Digital Fill Agent','Digital DRC Agent','Digital LVS Agent','Digital Tapeout Agent','Digital Tapeout Logic Equivalence Check Agent','Digital Executive Summary Agent'
    ]::text[])
),
defs as (
  select
    name,
    loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id','n'||ord,
            'type','agentNode',
            'position',jsonb_build_object(
              'x',80+((ord-1)%4)*372,
              'y',160+floor((ord-1)/4)*210
            ),
            'data',jsonb_build_object(
              'desc','Auto-composed: '||agent,
              'uiLabel',agent,
              'backendLabel',agent
            )
          )
          order by ord
        )
        from unnest(agents) with ordinality as u(agent, ord)
      ),
      'edges',
      (
        select coalesce(
          jsonb_agg(
            jsonb_build_object(
              'id','e'||ord,
              'source','n'||ord,
              'target','n'||(ord+1)
            )
            order by ord
          ),
          '[]'::jsonb
        )
        from generate_series(1, greatest(array_length(agents, 1) - 1, 0)) as ord
      )
    ) as definitions
  from template
)
update public.workflows w
set
  loop_type = defs.loop_type,
  definitions = defs.definitions,
  nodes = defs.definitions->'nodes',
  edges = defs.definitions->'edges',
  is_prebuilt = true,
  updated_at = now()
from defs
where w.name = defs.name and coalesce(w.is_prebuilt, false) = true;
