-- System RTL dashboard agent and template wiring.
-- Safe to re-run. No ON CONFLICT is used.

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

with agent_template as (
  select
    'System RTL Evidence Dashboard Agent'::text as agent_name,
    'System RTL Evidence Dashboard Agent'::text as name,
    'system'::text as loop_type,
    'system'::text as domain,
    'System RTL dashboard evidence.'::text as description,
    'agents.system.system_rtl_dashboard_agent:run_agent'::text as script_path,
    'agents.system.system_rtl_dashboard_agent:run_agent'::text as entrypoint,
    'native'::text as execution_mode,
    jsonb_build_array(
      'system/package/system_rtl_package.json',
      'system/integration/system_rtl_filelist_sim.txt',
      'system/integration/system_rtl_filelist_phys.txt',
      'system/integration/system_full_compile_summary.json'
    ) as inputs,
    jsonb_build_array(
      'system/dashboard/system_rtl_dashboard.json',
      'system/dashboard/SYSTEM_RTL_DASHBOARD.md'
    ) as outputs,
    jsonb_build_array(
      'system/dashboard/system_rtl_dashboard.json',
      'system/dashboard/SYSTEM_RTL_DASHBOARD.md'
    ) as artifact_paths,
    jsonb_build_array('structured_data', 'report') as artifact_types,
    jsonb_build_array('rtl_analysis', 'artifact_publish', 'dashboard_evidence') as required_skills,
    jsonb_build_array('python', 'supabase') as required_tools,
    jsonb_build_array(
      'pre_run_validate_inputs',
      'post_run_collect_artifacts',
      'post_run_update_state',
      'on_failure_capture_traceback',
      'on_failure_preserve_logs',
      'artifact_publish_to_supabase'
    ) as hooks,
    jsonb_build_object(
      'registry_source', 'SYSTEM_AGENT_FUNCTIONS',
      'variable', 'system_rtl_dashboard_agent',
      'studio_contract_version', 'v1'
    ) as metadata,
    jsonb_build_object(
      'name', 'System RTL Evidence Dashboard Agent',
      'loop_type', 'system',
      'domain', 'system',
      'entrypoint', 'agents.system.system_rtl_dashboard_agent:run_agent',
      'execution_mode', 'native'
    ) as agent_spec
),
updated_agent as (
  update public.agents as agent
  set agent_name = agent_template.agent_name,
      name = agent_template.name,
      loop_type = agent_template.loop_type,
      domain = agent_template.domain,
      description = agent_template.description,
      script_path = agent_template.script_path,
      entrypoint = agent_template.entrypoint,
      execution_mode = agent_template.execution_mode,
      inputs = agent_template.inputs,
      outputs = agent_template.outputs,
      artifact_paths = agent_template.artifact_paths,
      artifact_types = agent_template.artifact_types,
      required_skills = agent_template.required_skills,
      required_tools = agent_template.required_tools,
      agent_spec = agent_template.agent_spec,
      skills = agent_template.required_skills,
      tools = agent_template.required_tools,
      hooks = agent_template.hooks,
      metadata = agent_template.metadata,
      owner_id = null,
      is_custom = false,
      is_prebuilt = true,
      is_marketplace = false,
      status = 'approved',
      visibility = 'global',
      source = 'platform_registry',
      updated_at = now()
  from agent_template
  where (agent.agent_name = agent_template.agent_name or agent.name = agent_template.name)
    and agent.owner_id is null
  returning agent.name
)
insert into public.agents (
  agent_name,
  name,
  loop_type,
  domain,
  description,
  script_path,
  entrypoint,
  execution_mode,
  inputs,
  outputs,
  artifact_paths,
  artifact_types,
  required_skills,
  required_tools,
  agent_spec,
  skills,
  tools,
  hooks,
  metadata,
  owner_id,
  is_custom,
  is_prebuilt,
  is_marketplace,
  status,
  visibility,
  source,
  created_at,
  updated_at
)
select
  agent_template.agent_name,
  agent_template.name,
  agent_template.loop_type,
  agent_template.domain,
  agent_template.description,
  agent_template.script_path,
  agent_template.entrypoint,
  agent_template.execution_mode,
  agent_template.inputs,
  agent_template.outputs,
  agent_template.artifact_paths,
  agent_template.artifact_types,
  agent_template.required_skills,
  agent_template.required_tools,
  agent_template.agent_spec,
  agent_template.required_skills,
  agent_template.required_tools,
  agent_template.hooks,
  agent_template.metadata,
  null,
  false,
  true,
  false,
  'approved',
  'global',
  'platform_registry',
  now(),
  now()
from agent_template
where not exists (
  select 1
  from public.agents as agent
  where (agent.agent_name = agent_template.agent_name or agent.name = agent_template.name)
    and agent.owner_id is null
);

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template_agents(name, loop_type, agents) as (
  values
    ('System_RTL', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent'
    ]::text[]),
    ('System_Synthesis', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent'
    ]::text[]),
    ('System_PD', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent',
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
    ]::text[])
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
  from public.workflows as workflow
  where workflow.name = template_defs.name
    and workflow.user_id is null
);

with template_agents(name, loop_type, agents) as (
  values
    ('System_RTL', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent'
    ]::text[]),
    ('System_Synthesis', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent'
    ]::text[]),
    ('System_PD', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent',
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
    ]::text[])
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
    nodes = template_defs.definitions->'nodes',
    edges = template_defs.definitions->'edges',
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
from template_defs
where workflow.name = template_defs.name
  and workflow.user_id is null;

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
