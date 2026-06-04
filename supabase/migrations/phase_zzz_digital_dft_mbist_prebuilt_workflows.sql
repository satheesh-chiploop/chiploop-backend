-- Publish the DFT/ATPG/MBIST digital templates to Supabase.
--
-- Supabase is the source of truth for Studio-visible workflow templates.
-- This migration intentionally updates only shared platform templates
-- (user_id is null) and keeps user-owned workflow executions private.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with templates(name, loop_type, agents) as (
  values
    ('Digital_Arch2RTL', 'digital', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital RTL Agent',
      'Digital Power Intent (UPF-lite) Agent',
      'Digital UPF Static Check Agent',
      'Digital IP Packaging & Handoff Agent',
      'Digital Arch2RTL Dashboard Agent'
    ]::text[]),
    ('Digital_Arch2Synthesis', 'digital', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital Clock & Reset Architecture Agent',
      'Digital Power Intent (UPF-lite) Agent',
      'Digital UPF Static Check Agent',
      'Digital RTL Agent',
      'Digital IP Packaging & Handoff Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent'
    ]::text[]),
    ('Digital_Arch2Tapeout', 'digital', array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital Clock & Reset Architecture Agent',
      'Digital Power Intent (UPF-lite) Agent',
      'Digital UPF Static Check Agent',
      'Digital RTL Agent',
      'Digital IP Packaging & Handoff Agent',
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
      'Digital Executive Summary Agent'
    ]::text[]),
    ('Digital_Verify_Closure', 'digital', array[
      'Digital Verify Closure Ingest Agent',
      'Digital Coverage Gap Analysis Agent',
      'Digital Failure Triage Agent',
      'Digital Closure Recommendation Agent'
    ]::text[]),
    ('System_Product_App_Builder', 'system', array[
      'System Product Collateral Ingest Agent',
      'System Product Capability Model Agent',
      'System Product Dashboard Agent',
      'System Product Package Agent'
    ]::text[])
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
  from templates t
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

-- Keep the known platform template rows visible in Studio. App executions are
-- user-owned and are deliberately excluded by the user_id is null predicate.
with platform_templates(name) as (
  values
    ('Digital_Arch2RTL'),
    ('Digital_Arch2Sim'),
    ('Digital_Arch2Synthesis'),
    ('Digital_Arch2Tapeout'),
    ('Digital_DQA'),
    ('Digital_Verify'),
    ('Digital_Verify_Closure'),
    ('Digital_Smoke'),
    ('Digital_Integrate'),
    ('Analog_Run'),
    ('Analog_SpecBuilder'),
    ('Analog_NetlistScaffold'),
    ('Analog_BehavioralModel'),
    ('Analog_ModelValidation'),
    ('Analog_Correlation'),
    ('Analog_Iterate'),
    ('Analog_Abstracts'),
    ('Embedded_Run'),
    ('Embedded_HAL'),
    ('Embedded_Driver'),
    ('Embedded_Boot'),
    ('Embedded_Diagnostics'),
    ('Embedded_LogAnalyzer'),
    ('Embedded_Validate'),
    ('System_End2End'),
    ('System_Architecture_Explorer'),
    ('System_Cache_Tuning'),
    ('System_ISA_Compare'),
    ('System_Memory_Bottleneck'),
    ('System_CPU_Model'),
    ('System_Architecture_to_RTL_Delivery'),
    ('System_Sim'),
    ('System_RTL'),
    ('System_PD'),
    ('System_Firmware'),
    ('System_Software'),
    ('System_Software_Validation_L1'),
    ('System_Software_Validation_L2'),
    ('System_Product_App_Builder'),
    ('Validation_Run'),
    ('Validation_PlanCoverage'),
    ('Validation_BenchSetup'),
    ('Validation_Preflight'),
    ('Validation_Insights')
)
update public.workflows as workflow
set is_prebuilt = true,
    status = 'saved',
    updated_at = now()
from platform_templates as template
where workflow.name = template.name
  and workflow.user_id is null;

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;

-- Verification query:
-- select name, loop_type, is_prebuilt, user_id, jsonb_array_length(definitions->'nodes') as agent_count
-- from public.workflows
-- where user_id is null and is_prebuilt = true
-- order by loop_type, name;
