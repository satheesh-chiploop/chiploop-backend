-- Add opt-in Arch2RTL MBIST RTL insertion.
-- Supabase is the source of truth. Avoid ON CONFLICT because deployed
-- projects may not have matching unique constraints.

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
  add column if not exists created_at timestamptz default now(),
  add column if not exists updated_at timestamptz default now();

with agent_template as (
  select
    'Digital MBIST RTL Insertion Agent'::text as name,
    'digital'::text as loop_type,
    'dft'::text as domain,
    'Optional Arch2RTL-stage AutoMBIST flow. Detects OpenRAM/SRAM RTL, generates MBIST wrapper RTL, runs standalone AutoMBIST simulation, and stages MBIST RTL for handoff only when enabled by the user.'::text as description,
    'agents.digital.digital_mbist_rtl_insertion_agent:run_agent'::text as entrypoint
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
    inputs = '["rtl/*.v","rtl/*.sv","digital/rtl/*.v","digital/rtl/*.sv"]'::jsonb,
    outputs = '["digital/mbist_rtl_insertion/config.yml","digital/mbist_rtl_insertion/autombist_run.log","digital/mbist_rtl_insertion/simulate.log","digital/mbist_rtl_insertion/autombist_report.txt","digital/mbist_rtl_insertion/autombist_latest.json","digital/mbist_rtl_insertion/mbist_rtl_insertion_summary.json","digital/mbist_rtl_insertion/integrated_rtl/**/*.v","digital/mbist_rtl_insertion/integrated_rtl/**/*.sv"]'::jsonb,
    artifact_paths = '["digital/mbist_rtl_insertion/config.yml","digital/mbist_rtl_insertion/autombist_run.log","digital/mbist_rtl_insertion/simulate.log","digital/mbist_rtl_insertion/autombist_report.txt","digital/mbist_rtl_insertion/autombist_latest.json","digital/mbist_rtl_insertion/mbist_rtl_insertion_summary.json","digital/mbist_rtl_insertion/integrated_rtl/**/*.v","digital/mbist_rtl_insertion/integrated_rtl/**/*.sv"]'::jsonb,
    artifact_types = '["rtl","structured_data","simulation_log"]'::jsonb,
    required_skills = '["artifact_publish","dft_mbist","rtl_generation"]'::jsonb,
    required_tools = '["python","supabase","autombist","iverilog","cocotb"]'::jsonb,
    skills = '["artifact_publish","dft_mbist","rtl_generation"]'::jsonb,
    tools = '["python","supabase","autombist","iverilog","cocotb"]'::jsonb,
    hooks = '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
    metadata = jsonb_build_object('registry_source','MBIST_RTL_INSERTION_MIGRATION','studio_contract_version','v1','enabled_by_toggle','insert_mbist'),
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
  is_custom, is_prebuilt, is_marketplace, status, visibility, source, created_at, updated_at
)
select
  name, name, loop_type, domain, description, entrypoint, entrypoint,
  'legacy_adapter',
  '["rtl/*.v","rtl/*.sv","digital/rtl/*.v","digital/rtl/*.sv"]'::jsonb,
  '["digital/mbist_rtl_insertion/config.yml","digital/mbist_rtl_insertion/autombist_run.log","digital/mbist_rtl_insertion/simulate.log","digital/mbist_rtl_insertion/autombist_report.txt","digital/mbist_rtl_insertion/autombist_latest.json","digital/mbist_rtl_insertion/mbist_rtl_insertion_summary.json","digital/mbist_rtl_insertion/integrated_rtl/**/*.v","digital/mbist_rtl_insertion/integrated_rtl/**/*.sv"]'::jsonb,
  '["digital/mbist_rtl_insertion/config.yml","digital/mbist_rtl_insertion/autombist_run.log","digital/mbist_rtl_insertion/simulate.log","digital/mbist_rtl_insertion/autombist_report.txt","digital/mbist_rtl_insertion/autombist_latest.json","digital/mbist_rtl_insertion/mbist_rtl_insertion_summary.json","digital/mbist_rtl_insertion/integrated_rtl/**/*.v","digital/mbist_rtl_insertion/integrated_rtl/**/*.sv"]'::jsonb,
  '["rtl","structured_data","simulation_log"]'::jsonb,
  '["artifact_publish","dft_mbist","rtl_generation"]'::jsonb,
  '["python","supabase","autombist","iverilog","cocotb"]'::jsonb,
  '["artifact_publish","dft_mbist","rtl_generation"]'::jsonb,
  '["python","supabase","autombist","iverilog","cocotb"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','MBIST_RTL_INSERTION_MIGRATION','studio_contract_version','v1','enabled_by_toggle','insert_mbist'),
  false, true, false, 'approved', 'global', 'platform_registry', now(), now()
from agent_template
where not exists (
  select 1 from public.agents agent
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
);

with workflow_template as (
  select 'Digital_Arch2RTL'::text as name, 'digital'::text as loop_type, array[
    'Digital Spec Agent',
    'Digital Architecture Agent',
    'Digital Microarchitecture Agent',
    'Digital Register Map Agent',
    'Digital RTL Agent',
    'Digital MBIST RTL Insertion Agent',
    'Digital Power Intent (UPF-lite) Agent',
    'Digital UPF Static Check Agent',
    'Digital IP Packaging & Handoff Agent',
    'Digital Arch2RTL Dashboard Agent'
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
    and workflow.user_id is null
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
      and workflow.user_id is null
  );

create table if not exists public.product_stage_schemas (
  id uuid primary key default gen_random_uuid(),
  app text not null unique,
  schema jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

update public.product_stage_schemas
set
  schema = jsonb_set(
    schema,
    '{fields}',
    coalesce(schema->'fields', '[]'::jsonb) ||
      '[{"key":"insert_mbist","label":"Insert MBIST","type":"boolean","defaultValue":false}]'::jsonb
  ),
  updated_at = now()
where app = 'Digital_Arch2RTL'
  and not exists (
    select 1
    from jsonb_array_elements(coalesce(schema->'fields', '[]'::jsonb)) field
    where field->>'key' = 'insert_mbist'
  );
