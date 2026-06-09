-- Register System Sim closure templates and System-labeled closure agent aliases.
-- Safe to re-run. Avoids ON CONFLICT because older environments may not have
-- unique constraints on public.agents/public.workflows template names.

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
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists nodes jsonb,
  add column if not exists edges jsonb,
  add column if not exists definitions jsonb,
  add column if not exists loop_type text;

with agent_template(agent_name, entrypoint, description) as (
  values
    ('System Sim Closure Ingest Agent', 'agents.digital.digital_verify_closure_ingest_agent:run_agent', 'Loads baseline System Sim coverage, simulation, testcase, plan, and RTL handoff artifacts for closure analysis.'),
    ('System Sim Coverage Gap Analysis Agent', 'agents.digital.digital_coverage_gap_analysis_agent:run_agent', 'Analyzes System Sim functional and code coverage gaps and failing seeds.'),
    ('System Sim Failure Triage Agent', 'agents.digital.digital_failure_triage_agent:run_agent', 'Classifies failing System Sim testcase and seed pairs before coverage chasing.'),
    ('System Sim Failure Debug Agent', 'agents.digital.digital_failure_debug_agent:run_agent', 'Performs log-first System Sim failure debug and proposal-only fix guidance.'),
    ('System Sim Closure Recommendation Agent', 'agents.digital.digital_closure_recommendation_agent:run_agent', 'Recommends next System Sim closure actions from coverage and failure evidence.'),
    ('System Sim Verification Plan Update Agent', 'agents.digital.digital_verification_plan_update_agent:run_agent', 'Updates the System Sim verification plan from closure gaps.'),
    ('System Sim Coverage Plan Update Agent', 'agents.digital.digital_coverage_plan_update_agent:run_agent', 'Updates the System Sim coverage plan from uncovered functional and code coverage points.'),
    ('System Sim Testcase Seed Update Agent', 'agents.digital.digital_testcase_seed_update_agent:run_agent', 'Adds System Sim testcase intents and seed budgets for closure iterations.'),
    ('System Sim Closure Rerun Planner Agent', 'agents.digital.digital_closure_rerun_planner_agent:run_agent', 'Plans the next System Sim rerun mode and coverage-targeted seed set.'),
    ('System Sim Closure Iteration Judge Agent', 'agents.digital.digital_closure_iteration_judge_agent:run_agent', 'Judges whether System Sim closure iterations should continue or stop.')
),
updated_agent as (
  update public.agents a
  set agent_name = t.agent_name,
      name = t.agent_name,
      loop_type = 'system',
      domain = 'system',
      description = t.description,
      script_path = t.entrypoint,
      entrypoint = t.entrypoint,
      execution_mode = 'native',
      inputs = '["system/sim/system_sim_dashboard.json", "system/sim/system_sim_execution.json", "vv/tb/reports"]'::jsonb,
      outputs = '["verify_closure", "system/sim"]'::jsonb,
      artifact_paths = '["verify_closure", "system/sim"]'::jsonb,
      artifact_types = '["structured_data", "report"]'::jsonb,
      required_skills = '["system_sim_closure", "coverage_analysis", "artifact_publish"]'::jsonb,
      required_tools = '["python", "supabase"]'::jsonb,
      skills = '["system_sim_closure", "coverage_analysis", "artifact_publish"]'::jsonb,
      tools = '["python", "supabase"]'::jsonb,
      hooks = '["pre_run_validate_inputs", "post_run_collect_artifacts", "post_run_update_state", "artifact_publish_to_supabase"]'::jsonb,
      metadata = jsonb_build_object('registry_source', 'SYSTEM_AGENT_FUNCTIONS', 'studio_contract_version', 'v1'),
      agent_spec = jsonb_build_object('name', t.agent_name, 'loop_type', 'system', 'domain', 'system', 'entrypoint', t.entrypoint, 'execution_mode', 'native'),
      owner_id = null,
      is_custom = false,
      is_prebuilt = true,
      is_marketplace = false,
      status = 'approved',
      visibility = 'global',
      source = 'platform_registry',
      updated_at = now()
  from agent_template t
  where (a.agent_name = t.agent_name or a.name = t.agent_name)
    and a.owner_id is null
  returning a.name
)
insert into public.agents (
  agent_name, name, loop_type, domain, description, script_path, entrypoint, execution_mode,
  inputs, outputs, artifact_paths, artifact_types, required_skills, required_tools,
  agent_spec, skills, tools, hooks, metadata, owner_id, is_custom, is_prebuilt,
  is_marketplace, status, visibility, source, created_at, updated_at
)
select
  t.agent_name,
  t.agent_name,
  'system',
  'system',
  t.description,
  t.entrypoint,
  t.entrypoint,
  'native',
  '["system/sim/system_sim_dashboard.json", "system/sim/system_sim_execution.json", "vv/tb/reports"]'::jsonb,
  '["verify_closure", "system/sim"]'::jsonb,
  '["verify_closure", "system/sim"]'::jsonb,
  '["structured_data", "report"]'::jsonb,
  '["system_sim_closure", "coverage_analysis", "artifact_publish"]'::jsonb,
  '["python", "supabase"]'::jsonb,
  jsonb_build_object('name', t.agent_name, 'loop_type', 'system', 'domain', 'system', 'entrypoint', t.entrypoint, 'execution_mode', 'native'),
  '["system_sim_closure", "coverage_analysis", "artifact_publish"]'::jsonb,
  '["python", "supabase"]'::jsonb,
  '["pre_run_validate_inputs", "post_run_collect_artifacts", "post_run_update_state", "artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source', 'SYSTEM_AGENT_FUNCTIONS', 'studio_contract_version', 'v1'),
  null,
  false,
  true,
  false,
  'approved',
  'global',
  'platform_registry',
  now(),
  now()
from agent_template t
where not exists (
  select 1
  from public.agents a
  where (a.agent_name = t.agent_name or a.name = t.agent_name)
    and a.owner_id is null
);

with template(name, loop_type, agents) as (
  values
    ('System_Sim_Closure', 'system', array[
      'System Sim Closure Ingest Agent',
      'System Sim Coverage Gap Analysis Agent',
      'System Sim Failure Triage Agent',
      'System Sim Failure Debug Agent',
      'System Sim Closure Recommendation Agent'
    ]::text[]),
    ('System_Sim_Closure_Loop', 'system', array[
      'System Sim Closure Ingest Agent',
      'System Sim Coverage Gap Analysis Agent',
      'System Sim Failure Triage Agent',
      'System Sim Failure Debug Agent',
      'System Sim Closure Recommendation Agent',
      'System Sim Verification Plan Update Agent',
      'System Sim Coverage Plan Update Agent',
      'System Sim Testcase Seed Update Agent',
      'System Sim Closure Rerun Planner Agent',
      'System Assertions (SVA) Agent',
      'System Testbench Generator Agent',
      'System Functional Coverage Agent',
      'System Simulation Control Agent',
      'System Simulation Execution Agent',
      'System Simulation Coverage Summary Agent',
      'System Sim Closure Iteration Judge Agent'
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
            'position', jsonb_build_object('x', 80 + (((ord - 1) % 4) * 372), 'y', 160 + (((ord - 1) / 4) * 210)),
            'data', jsonb_build_object('desc', 'Auto-composed: ' || agent_name, 'uiLabel', agent_name, 'backendLabel', agent_name)
          )
          order by ord
        )
        from unnest(t.agents) with ordinality as a(agent_name, ord)
      ),
      'edges',
      coalesce(
        (
          select jsonb_agg(jsonb_build_object('id', 'e' || ord, 'source', 'n' || ord, 'target', 'n' || (ord + 1)) order by ord)
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      )
    ) as definitions
  from template t
),
updated_workflow as (
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

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
