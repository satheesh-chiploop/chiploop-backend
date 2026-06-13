-- Reassert System PD physical signoff sequence with post-route and post-fill STA.
-- Supabase remains the source of truth for prebuilt workflow sequencing.

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

with agent_template as (
  select
    'Digital STA PostFill Agent'::text as name,
    'digital'::text as loop_type,
    'physical_design'::text as domain,
    'Runs OpenLane2 post-fill signoff STA using OpenROAD.STAPostPNR after fill insertion. Exports metrics.json.'::text as description,
    'agents.digital.digital_sta_postfill_agent:run_agent'::text as entrypoint,
    'legacy_adapter'::text as execution_mode,
    array['digital/constraints/top.sdc','digital/fill/primary.def','digital/foundry/openlane/config.json']::text[] as inputs,
    array['digital/sta_postfill/config.json','digital/sta_postfill/constraints/top.sdc','digital/sta_postfill/run.sh','digital/sta_postfill/logs/openlane_sta_postfill.log','digital/sta_postfill/metrics.json','digital/sta_postfill/sta_postfill_summary.json','digital/sta_postfill/sta_postfill_summary.md']::text[] as outputs,
    array['digital/sta_postfill/config.json','digital/sta_postfill/constraints/top.sdc','digital/sta_postfill/run.sh','digital/sta_postfill/logs/openlane_sta_postfill.log','digital/sta_postfill/metrics.json','digital/sta_postfill/sta_postfill_summary.json','digital/sta_postfill/sta_postfill_summary.md']::text[] as artifact_paths,
    array['implementation_artifact','report','structured_data']::text[] as artifact_types,
    array['artifact_publish','openlane_config_generation','sta_constraint_generation']::text[] as required_skills,
    array['openlane','openroad','python','supabase']::text[] as required_tools
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
    execution_mode = agent_template.execution_mode,
    inputs = to_jsonb(agent_template.inputs),
    outputs = to_jsonb(agent_template.outputs),
    artifact_paths = to_jsonb(agent_template.artifact_paths),
    artifact_types = to_jsonb(agent_template.artifact_types),
    required_skills = to_jsonb(agent_template.required_skills),
    required_tools = to_jsonb(agent_template.required_tools),
    skills = to_jsonb(agent_template.required_skills),
    tools = to_jsonb(agent_template.required_tools),
    hooks = '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
    metadata = jsonb_build_object('registry_source','SYSTEM_PD_POSTFILL_STA_MIGRATION','studio_contract_version','v1'),
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
  execution_mode, to_jsonb(inputs), to_jsonb(outputs), to_jsonb(artifact_paths), to_jsonb(artifact_types),
  to_jsonb(required_skills), to_jsonb(required_tools), to_jsonb(required_skills), to_jsonb(required_tools),
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','SYSTEM_PD_POSTFILL_STA_MIGRATION','studio_contract_version','v1'),
  false, true, false, 'approved', 'global', 'platform_registry', now()
from agent_template
where not exists (
  select 1
  from public.agents agent
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
);

with
workflow_template as (
  select
    'System_PD'::text as name,
    'system'::text as loop_type,
    array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec2RTL Conformance Agent',
      'Digital UPF Static Check Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent',
      'Analog Macro Interface Contract Agent',
      'Analog Macro Interface Validation Agent',
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
      'Digital Executive Summary Agent'
    ]::text[] as agents
),
expanded as (
  select
    wt.name,
    wt.loop_type,
    wt.agents,
    jsonb_agg(jsonb_build_object('id', lower(regexp_replace(agent, '[^a-zA-Z0-9]+', '_', 'g')), 'agent', agent, 'name', agent) order by ord) as nodes,
    jsonb_agg(jsonb_build_object('from', lower(regexp_replace(prev_agent, '[^a-zA-Z0-9]+', '_', 'g')), 'to', lower(regexp_replace(agent, '[^a-zA-Z0-9]+', '_', 'g'))) order by ord) filter (where prev_agent is not null) as edges
  from workflow_template wt
  cross join lateral unnest(wt.agents) with ordinality as a(agent, ord)
  left join lateral (
    select wt.agents[ord - 1] as prev_agent
  ) p on ord > 1
  group by wt.name, wt.loop_type, wt.agents
),
updated_workflows as (
  update public.workflows workflow
  set
    loop_type = expanded.loop_type,
    definitions = jsonb_build_object('agents', to_jsonb(expanded.agents), 'source', 'system_pd_postfill_sta_source_of_truth'),
    nodes = expanded.nodes,
    edges = coalesce(expanded.edges, '[]'::jsonb),
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
  from expanded
  where workflow.name = expanded.name
    and coalesce(workflow.is_prebuilt, false) = true
    and (workflow.user_id is null)
  returning workflow.name
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(),
  null,
  name,
  loop_type,
  jsonb_build_object('agents', to_jsonb(agents), 'source', 'system_pd_postfill_sta_source_of_truth'),
  nodes,
  coalesce(edges, '[]'::jsonb),
  'saved',
  true,
  now(),
  now()
from expanded
where not exists (
  select 1
  from public.workflows workflow
  where workflow.name = expanded.name
    and workflow.user_id is null
);
