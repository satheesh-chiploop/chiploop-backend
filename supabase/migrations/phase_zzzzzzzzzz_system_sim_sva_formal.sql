-- Adds System Sim SVA/Formal source-of-truth entries without relying on ON CONFLICT.
-- Matches the current source-of-truth schema: public.agents and public.workflows.

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
    'System Formal Verification Agent'::text as agent_name,
    'System Formal Verification Agent'::text as name,
    'system'::text as loop_type,
    'system'::text as domain,
    'Runs optional system-level SymbiYosys formal setup against the hydrated System RTL top and reports real tool availability/pass/fail evidence.'::text as description,
    'agents.system.system_formal_verification_agent:run_agent'::text as script_path,
    'agents.system.system_formal_verification_agent:run_agent'::text as entrypoint,
    'legacy_adapter'::text as execution_mode,
    to_jsonb(array['system/integration/soc_top_sim.sv','system/integration/system_rtl_filelist_sim.txt','vv/tb/sva_spec.json(optional)','*.v','*.sv']::text[]) as inputs,
    to_jsonb(array['vv/formal/formal_report.json','vv/formal/*.sby','vv/system_formal_verification_agent.log']::text[]) as outputs,
    to_jsonb(array['vv/formal/formal_report.json','vv/formal/*.sby','vv/system_formal_verification_agent.log']::text[]) as artifact_paths,
    to_jsonb(array['report','structured_data']::text[]) as artifact_types,
    to_jsonb(array['artifact_publish','formal_verification']::text[]) as required_skills,
    to_jsonb(array['python','supabase','yosys','sby','z3']::text[]) as required_tools,
    to_jsonb(array[
      'pre_run_validate_inputs',
      'post_run_collect_artifacts',
      'post_run_update_state',
      'on_failure_capture_traceback',
      'on_failure_preserve_logs',
      'artifact_publish_to_supabase'
    ]::text[]) as hooks,
    jsonb_build_object(
      'registry_source', 'SYSTEM_AGENT_FUNCTIONS',
      'variable', 'system_formal_verification_agent',
      'studio_contract_version', 'v1'
    ) as metadata,
    jsonb_build_object(
      'name', 'System Formal Verification Agent',
      'loop_type', 'system',
      'domain', 'system',
      'entrypoint', 'agents.system.system_formal_verification_agent:run_agent',
      'execution_mode', 'legacy_adapter'
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
  required_skills,
  required_tools,
  hooks,
  metadata,
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

with template as (
  select
    'System_Sim'::text as name,
    'system'::text as loop_type,
    array[
      'System CoSim Ingest Agent',
      'System Assertions (SVA) Agent',
      'System Testbench Generator Agent',
      'System Functional Coverage Agent',
      'System Simulation Control Agent',
      'System Simulation Execution Agent',
      'System Simulation Coverage Summary Agent'
    ]::text[] as agents
),
definition as (
  select
    template.name,
    template.loop_type,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'label', agent_name,
            'type', 'agent',
            'position', jsonb_build_object('x', ((ord - 1) * 220), 'y', 0),
            'data', jsonb_build_object('label', agent_name, 'backendLabel', agent_name)
          )
          order by ord
        )
        from unnest(template.agents) with ordinality as agent_list(agent_name, ord)
      ),
      'edges',
      (
        select coalesce(jsonb_agg(
          jsonb_build_object(
            'id', 'e' || ord || '_' || (ord + 1),
            'source', 'n' || ord,
            'target', 'n' || (ord + 1)
          )
          order by ord
        ), '[]'::jsonb)
        from generate_series(1, greatest(array_length(template.agents, 1) - 1, 0)) as ord
      )
    ) as definitions
  from template
),
updated_workflow as (
  update public.workflows as workflow
  set definitions = definition.definitions,
      nodes = definition.definitions->'nodes',
      edges = definition.definitions->'edges',
      loop_type = definition.loop_type,
      is_prebuilt = true,
      user_id = null,
      status = 'saved',
      updated_at = now()
  from definition
  where workflow.name = definition.name
    and workflow.user_id is null
  returning workflow.name
)
insert into public.workflows (
  id,
  user_id,
  name,
  loop_type,
  definitions,
  nodes,
  edges,
  status,
  is_prebuilt,
  created_at,
  updated_at
)
select
  gen_random_uuid(),
  null,
  definition.name,
  definition.loop_type,
  definition.definitions,
  definition.definitions->'nodes',
  definition.definitions->'edges',
  'saved',
  true,
  now(),
  now()
from definition
where not exists (
  select 1
  from public.workflows as workflow
  where workflow.name = definition.name
    and workflow.user_id is null
);

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
