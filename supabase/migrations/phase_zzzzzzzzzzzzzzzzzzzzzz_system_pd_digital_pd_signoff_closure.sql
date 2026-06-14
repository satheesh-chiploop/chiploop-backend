-- Add bounded signoff closure controllers for Digital Arch2Tapeout and System PD.
-- Avoid ON CONFLICT because deployed Supabase projects may not have unique
-- constraints on workflow or agent names.

alter table if exists public.agents
  add column if not exists agent_name text,
  add column if not exists name text,
  add column if not exists loop_type text,
  add column if not exists domain text,
  add column if not exists description text,
  add column if not exists script_path text,
  add column if not exists entrypoint text,
  add column if not exists execution_mode text default 'native',
  add column if not exists inputs jsonb,
  add column if not exists outputs jsonb,
  add column if not exists artifact_paths jsonb,
  add column if not exists artifact_types jsonb,
  add column if not exists required_skills jsonb,
  add column if not exists required_tools jsonb,
  add column if not exists agent_spec jsonb,
  add column if not exists skills jsonb,
  add column if not exists tools jsonb,
  add column if not exists hooks jsonb,
  add column if not exists metadata jsonb,
  add column if not exists owner_id uuid,
  add column if not exists is_custom boolean not null default false,
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists is_marketplace boolean not null default false,
  add column if not exists status text default 'approved',
  add column if not exists visibility text default 'global',
  add column if not exists source text default 'platform_registry',
  add column if not exists created_at timestamptz default now(),
  add column if not exists updated_at timestamptz default now();

alter table if exists public.workflows
  add column if not exists definitions jsonb,
  add column if not exists nodes jsonb,
  add column if not exists edges jsonb,
  add column if not exists loop_type text,
  add column if not exists status text,
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists updated_at timestamptz default now();

alter table if exists public.product_stage_schemas
  add column if not exists app text,
  add column if not exists schema jsonb,
  add column if not exists is_active boolean not null default true,
  add column if not exists updated_at timestamptz default now();

with agent_template as (
  select * from (
    values
      (
        'Digital PD Signoff Closure Agent',
        'digital',
        'physical_design',
        'Analyzes Arch2Tapeout signoff evidence across PostFill STA, DRC, LVS, tapeout, synthesis LEC, and tapeout LEC. Produces a bounded closure restart plan without faking repair success.',
        'agents.digital.digital_pd_signoff_closure_agent:run_agent'
      ),
      (
        'System PD Signoff Closure Agent',
        'system',
        'physical_design',
        'Analyzes System PD signoff evidence across analog signoff, PostFill STA, DRC, LVS, tapeout, synthesis LEC, and tapeout LEC. Produces a bounded closure restart plan without faking repair success.',
        'agents.system.system_pd_signoff_closure_agent:run_agent'
      )
  ) as t(name, loop_type, domain, description, entrypoint)
),
updated_agents as (
  update public.agents agent
  set
    agent_name = agent_template.name,
    name = agent_template.name,
    loop_type = agent_template.loop_type,
    domain = agent_template.domain,
    description = agent_template.description,
    script_path = agent_template.entrypoint,
    entrypoint = agent_template.entrypoint,
    execution_mode = 'legacy_adapter',
    inputs = '["digital/executive_summary.json","digital/sta_postfill/metrics.json","digital/drc/drc_summary.json","digital/lvs/lvs_summary.json","digital/tapeout/tapeout_summary.json","digital/lec/lec_summary.json","digital/tapeout_lec/tapeout_lec_summary.json"]'::jsonb,
    outputs = '["digital/signoff_closure/signoff_closure_plan.json","digital/signoff_closure/signoff_closure_chart.json","digital/signoff_closure/signoff_closure_plan.md"]'::jsonb,
    artifact_paths = '["digital/signoff_closure/signoff_closure_plan.json","digital/signoff_closure/signoff_closure_chart.json","digital/signoff_closure/signoff_closure_plan.md"]'::jsonb,
    artifact_types = '["report","structured_data"]'::jsonb,
    required_skills = '["artifact_publish","signoff_triage"]'::jsonb,
    required_tools = '["python","supabase"]'::jsonb,
    skills = '["artifact_publish","signoff_triage"]'::jsonb,
    tools = '["python","supabase"]'::jsonb,
    hooks = '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
    metadata = jsonb_build_object('registry_source','SIGNOFF_CLOSURE_MIGRATION','studio_contract_version','v1'),
    is_custom = false,
    is_prebuilt = true,
    is_marketplace = false,
    status = 'approved',
    visibility = 'global',
    source = 'platform_registry',
    updated_at = now()
  from agent_template
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
  returning agent.name
)
insert into public.agents (
  agent_name, name, loop_type, domain, description, script_path, entrypoint,
  execution_mode, inputs, outputs, artifact_paths, artifact_types,
  required_skills, required_tools, skills, tools, hooks, metadata,
  is_custom, is_prebuilt, is_marketplace, status, visibility, source, updated_at
)
select
  name, name, loop_type, domain, description, entrypoint, entrypoint,
  'legacy_adapter',
  '["digital/executive_summary.json","digital/sta_postfill/metrics.json","digital/drc/drc_summary.json","digital/lvs/lvs_summary.json","digital/tapeout/tapeout_summary.json","digital/lec/lec_summary.json","digital/tapeout_lec/tapeout_lec_summary.json"]'::jsonb,
  '["digital/signoff_closure/signoff_closure_plan.json","digital/signoff_closure/signoff_closure_chart.json","digital/signoff_closure/signoff_closure_plan.md"]'::jsonb,
  '["digital/signoff_closure/signoff_closure_plan.json","digital/signoff_closure/signoff_closure_chart.json","digital/signoff_closure/signoff_closure_plan.md"]'::jsonb,
  '["report","structured_data"]'::jsonb,
  '["artifact_publish","signoff_triage"]'::jsonb,
  '["python","supabase"]'::jsonb,
  '["artifact_publish","signoff_triage"]'::jsonb,
  '["python","supabase"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','SIGNOFF_CLOSURE_MIGRATION','studio_contract_version','v1'),
  false, true, false, 'approved', 'global', 'platform_registry', now()
from agent_template
where not exists (
  select 1
  from public.agents agent
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
);

with workflow_template as (
  select 'Digital_Arch2Tapeout'::text as name, 'digital'::text as loop_type, array[
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
    'Digital STA PostFill Agent',
    'Digital DRC Agent',
    'Digital LVS Agent',
    'Digital Tapeout Agent',
    'Digital Tapeout Logic Equivalence Check Agent',
    'Digital Executive Summary Agent',
    'Digital PD Signoff Closure Agent'
  ]::text[] as agents
  union all
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
    wt.agents,
    jsonb_agg(
      jsonb_build_object(
        'id', 'n' || ord::text,
        'type', 'agentNode',
        'position', jsonb_build_object(
          'x', 80 + (((ord - 1) % 4) * 372),
          'y', 160 + (((ord - 1) / 4) * 210)
        ),
        'data', jsonb_build_object(
          'desc', 'Auto-composed: ' || agent,
          'uiLabel', agent,
          'backendLabel', agent
        )
      )
      order by ord
    ) as nodes,
    jsonb_agg(
      jsonb_build_object(
        'id', 'e' || (ord - 1)::text,
        'source', 'n' || (ord - 1)::text,
        'target', 'n' || ord::text
      )
      order by ord
    ) filter (where ord > 1) as edges
  from workflow_template wt
  cross join lateral unnest(wt.agents) with ordinality as a(agent, ord)
  group by wt.name, wt.loop_type, wt.agents
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
    select 1
    from public.workflows workflow
    where workflow.name = expanded.name
  );

with schema_template as (
  select
    'System_PD'::text as app,
    '{
      "fields": [
        {"key":"foundry","label":"Foundry","type":"text","defaultValue":"sky130"},
        {"key":"pdk","label":"PDK","type":"text","defaultValue":"sky130"},
        {"key":"analog_physical_mode","label":"Analog physical mode","type":"text","defaultValue":"blackbox"},
        {"key":"generate_analog_gds","label":"Generate analog GDS","type":"boolean","defaultValue":false},
        {"key":"regenerate_lef_lib_after_gds","label":"Regenerate LEF/LIB after GDS","type":"boolean","defaultValue":true},
        {"key":"run_lef_lib_consistency","label":"Run LEF/LIB consistency","type":"boolean","defaultValue":true},
        {"key":"run_logic_equivalence","label":"Run logic equivalence","type":"boolean","defaultValue":true},
        {"key":"run_drc","label":"Run DRC","type":"boolean","defaultValue":true},
        {"key":"run_lvs","label":"Run LVS","type":"boolean","defaultValue":true},
        {"key":"run_signoff_closure_loop","label":"Run signoff closure loop","type":"boolean","defaultValue":false},
        {"key":"max_signoff_closure_iterations","label":"Max signoff closure iterations","type":"number","defaultValue":1},
        {"key":"allow_timing_repair","label":"Allow timing repair","type":"boolean","defaultValue":true},
        {"key":"allow_drc_repair","label":"Allow DRC repair","type":"boolean","defaultValue":true},
        {"key":"allow_lvs_repair","label":"Allow LVS repair","type":"boolean","defaultValue":true},
        {"key":"allow_lec_repair","label":"Allow LEC repair","type":"boolean","defaultValue":true}
      ]
    }'::jsonb as schema
),
updated_schema as (
  update public.product_stage_schemas p
  set schema = schema_template.schema,
      is_active = true,
      updated_at = now()
  from schema_template
  where p.app = schema_template.app
  returning p.app
)
insert into public.product_stage_schemas (app, schema, is_active, updated_at)
select app, schema, true, now()
from schema_template
where not exists (select 1 from updated_schema)
  and not exists (
    select 1
    from public.product_stage_schemas p
    where p.app = schema_template.app
  );
