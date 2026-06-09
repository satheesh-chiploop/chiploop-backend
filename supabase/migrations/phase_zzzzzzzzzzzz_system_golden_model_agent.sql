-- Register System Golden Model Comparison Agent.
-- Safe to re-run. Uses update-then-insert instead of ON CONFLICT because
-- older ChipLoop Supabase schemas may not have uniqueness constraints.

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
    'System Golden Model Comparison Agent'::text as agent_name,
    'System Golden Model Comparison Agent'::text as name,
    'system'::text as loop_type,
    'system'::text as domain,
    'Generates System Sim scoreboard/golden-model collateral and reports scoreboard execution evidence without inventing expected behavior.'::text as description,
    'agents.system.system_golden_model_comparison_agent:run_agent'::text as entrypoint,
    'native'::text as execution_mode,
    '["vv/tb/tb_contract.json", "vv/tb/testcases.json", "system/integration/system_integration_intent.json"]'::jsonb as inputs,
    '["vv/tb/scoreboard.py", "vv/tb/golden_model_generation_report.json", "vv/tb/reports/scoreboard_report.json"]'::jsonb as outputs,
    '["vv/tb/scoreboard.py", "vv/tb/golden_model_generation_report.json", "vv/tb/reports/scoreboard_report.json"]'::jsonb as artifact_paths,
    '["verification_collateral", "structured_data", "report"]'::jsonb as artifact_types,
    '["system_sim", "golden_model", "scoreboard", "artifact_publish"]'::jsonb as required_skills,
    '["python", "cocotb", "supabase"]'::jsonb as required_tools,
    '["pre_run_validate_inputs", "post_run_collect_artifacts", "post_run_update_state", "artifact_publish_to_supabase"]'::jsonb as hooks
),
updated_agent as (
  update public.agents agent
  set agent_name = agent_template.agent_name,
      name = agent_template.name,
      loop_type = agent_template.loop_type,
      domain = agent_template.domain,
      description = agent_template.description,
      script_path = agent_template.entrypoint,
      entrypoint = agent_template.entrypoint,
      execution_mode = agent_template.execution_mode,
      inputs = agent_template.inputs,
      outputs = agent_template.outputs,
      artifact_paths = agent_template.artifact_paths,
      artifact_types = agent_template.artifact_types,
      required_skills = agent_template.required_skills,
      required_tools = agent_template.required_tools,
      skills = agent_template.required_skills,
      tools = agent_template.required_tools,
      hooks = agent_template.hooks,
      metadata = jsonb_build_object(
        'registry_source', 'SYSTEM_AGENT_FUNCTIONS',
        'studio_contract_version', 'v1',
        'optional_for_workflows', jsonb_build_array('System_Sim', 'System_Sim_Closure_Loop')
      ),
      agent_spec = jsonb_build_object(
        'name', agent_template.name,
        'loop_type', agent_template.loop_type,
        'domain', agent_template.domain,
        'entrypoint', agent_template.entrypoint,
        'execution_mode', agent_template.execution_mode,
        'optional_for_workflows', jsonb_build_array('System_Sim', 'System_Sim_Closure_Loop')
      ),
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
  agent_name, name, loop_type, domain, description, script_path, entrypoint, execution_mode,
  inputs, outputs, artifact_paths, artifact_types, required_skills, required_tools,
  agent_spec, skills, tools, hooks, metadata, owner_id, is_custom, is_prebuilt,
  is_marketplace, status, visibility, source, created_at, updated_at
)
select
  agent_name,
  name,
  loop_type,
  domain,
  description,
  entrypoint,
  entrypoint,
  execution_mode,
  inputs,
  outputs,
  artifact_paths,
  artifact_types,
  required_skills,
  required_tools,
  jsonb_build_object(
    'name', name,
    'loop_type', loop_type,
    'domain', domain,
    'entrypoint', entrypoint,
    'execution_mode', execution_mode,
    'optional_for_workflows', jsonb_build_array('System_Sim', 'System_Sim_Closure_Loop')
  ),
  required_skills,
  required_tools,
  hooks,
  jsonb_build_object(
    'registry_source', 'SYSTEM_AGENT_FUNCTIONS',
    'studio_contract_version', 'v1',
    'optional_for_workflows', jsonb_build_array('System_Sim', 'System_Sim_Closure_Loop')
  ),
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
  from public.agents agent
  where (agent.agent_name = agent_template.agent_name or agent.name = agent_template.name)
    and agent.owner_id is null
);
