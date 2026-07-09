-- Add native Digital Loop review/debug apps.
-- Supabase is the source of truth for prebuilt agents, workflows, and product-stage contracts.

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
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists status text default 'saved',
  add column if not exists updated_at timestamptz default now();

create table if not exists public.product_stage_schemas (
  id uuid primary key default gen_random_uuid(),
  app text not null unique,
  schema jsonb not null,
  is_active boolean not null default true,
  updated_at timestamptz not null default now()
);

with agent_templates(agent_name, description, entrypoint, inputs, outputs, variable, required_tools) as (
  values
    (
      'Digital RTL Review Agent',
      'Reviews RTL for handoff readiness, coding-risk patterns, module coverage, and implementation/verification recommendations.',
      'agents.digital.digital_rtl_review_agent:run_agent',
      '["rtl_text","pasted_rtl_files","repo_path","from_workflow_id","source_workflow_id"]'::jsonb,
      '["rtl_review/rtl_review.json","rtl_review/rtl_review.md"]'::jsonb,
      'digital_rtl_review_agent',
      '["python","supabase","verible_verilog_lint","slang","sv2v","verilator","yosys"]'::jsonb
    ),
    (
      'Digital Constraint Review Agent',
      'Reviews SDC constraints against RTL clock intent and flags missing clocks, IO timing, generated-clock, and exception risks.',
      'agents.digital.digital_constraint_review_agent:run_agent',
      '["constraints_sdc","clock_constraints","rtl_text","pasted_rtl_files","repo_path","from_workflow_id","source_workflow_id"]'::jsonb,
      '["constraint_review/constraint_review.json","constraint_review/constraint_review.md"]'::jsonb,
      'digital_constraint_review_agent',
      '["python","supabase","sta","yosys"]'::jsonb
    ),
    (
      'Digital Timing Debug Agent',
      'Analyzes STA/timing reports and classifies negative slack, unconstrained paths, fanout, setup, and hold debug actions.',
      'agents.digital.digital_timing_debug_agent:run_agent',
      '["timing_report_text","from_workflow_id","source_workflow_id","target_frequency_mhz","stage"]'::jsonb,
      '["timing_debug/timing_debug.json","timing_debug/timing_debug.md"]'::jsonb,
      'digital_timing_debug_agent',
      '["python","supabase","sta","openroad"]'::jsonb
    )
),
normalized_agents as (
  select
    agent_name,
    agent_name as name,
    'digital'::text as loop_type,
    'digital'::text as domain,
    description,
    entrypoint as script_path,
    entrypoint,
    'native'::text as execution_mode,
    inputs,
    outputs,
    outputs as artifact_paths,
    '["structured_data","report"]'::jsonb as artifact_types,
    '["digital_review","artifact_publish"]'::jsonb as required_skills,
    required_tools,
    '[
      "pre_run_validate_inputs",
      "post_run_collect_artifacts",
      "post_run_update_state",
      "on_failure_capture_traceback",
      "on_failure_preserve_logs",
      "artifact_publish_to_supabase"
    ]'::jsonb as hooks,
    jsonb_build_object(
      'registry_source', 'DIGITAL_AGENT_FUNCTIONS',
      'variable', variable,
      'studio_contract_version', 'v1'
    ) as metadata,
    jsonb_build_object(
      'name', agent_name,
      'loop_type', 'digital',
      'domain', 'digital',
      'entrypoint', entrypoint,
      'execution_mode', 'native'
    ) as agent_spec
  from agent_templates
),
updated_agents as (
  update public.agents a
  set agent_name = t.agent_name,
      name = t.name,
      loop_type = t.loop_type,
      domain = t.domain,
      description = t.description,
      script_path = t.script_path,
      entrypoint = t.entrypoint,
      execution_mode = t.execution_mode,
      inputs = t.inputs,
      outputs = t.outputs,
      artifact_paths = t.artifact_paths,
      artifact_types = t.artifact_types,
      required_skills = t.required_skills,
      required_tools = t.required_tools,
      agent_spec = t.agent_spec,
      skills = t.required_skills,
      tools = t.required_tools,
      hooks = t.hooks,
      metadata = t.metadata,
      owner_id = null,
      is_custom = false,
      is_prebuilt = true,
      is_marketplace = false,
      status = 'approved',
      visibility = 'global',
      source = 'platform_registry',
      updated_at = now()
  from normalized_agents t
  where coalesce(a.agent_name, a.name) = t.agent_name
  returning a.agent_name
)
insert into public.agents (
  agent_name, name, loop_type, domain, description, script_path, entrypoint, execution_mode,
  inputs, outputs, artifact_paths, artifact_types, required_skills, required_tools, agent_spec,
  skills, tools, hooks, metadata, owner_id, is_custom, is_prebuilt, is_marketplace,
  status, visibility, source, created_at, updated_at
)
select
  t.agent_name, t.name, t.loop_type, t.domain, t.description, t.script_path, t.entrypoint, t.execution_mode,
  t.inputs, t.outputs, t.artifact_paths, t.artifact_types, t.required_skills, t.required_tools, t.agent_spec,
  t.required_skills, t.required_tools, t.hooks, t.metadata, null, false, true, false,
  'approved', 'global', 'platform_registry', now(), now()
from normalized_agents t
where not exists (
  select 1 from public.agents a where coalesce(a.agent_name, a.name) = t.agent_name
);

with templates(name, description, agents) as (
  values
    ('Digital_RTL_Review', 'Reviews RTL handoff readiness and coding-risk evidence.', array['Digital RTL Handoff Ingest Agent','Digital RTL Review Agent']::text[]),
    ('Digital_Constraint_Review', 'Reviews SDC coverage against RTL clock intent.', array['Digital RTL Handoff Ingest Agent','Digital Constraint Review Agent']::text[]),
    ('Digital_Timing_Debug', 'Debugs timing and STA reports from synthesis or implementation.', array['Digital Timing Debug Agent']::text[])
),
definitions as (
  select
    t.name,
    t.description,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agentNode',
            'position', jsonb_build_object('x', 100 + ((ord - 1) * 350), 'y', 200),
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
          select jsonb_agg(jsonb_build_object('source', 'n' || ord, 'target', 'n' || (ord + 1)) order by ord)
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      ),
      'description',
      t.description
    ) as definitions
  from templates t
),
updated_workflows as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = 'digital',
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
  'digital',
  d.definitions,
  d.definitions->'nodes',
  d.definitions->'edges',
  'saved',
  true,
  now(),
  now()
from definitions d
where not exists (
  select 1 from public.workflows w where w.name = d.name and w.user_id is null
);

with schemas(app, schema) as (
  values
    (
      'Digital_RTL_Review',
      '{
        "note":"Review RTL from paste, repo path, or a prior workflow artifact.",
        "fields":[
          {"key":"rtl_source_mode","label":"RTL source","type":"select","defaultValue":"from_arch2rtl","options":[{"value":"from_arch2rtl","label":"Prior workflow"},{"value":"paste","label":"Paste RTL"},{"value":"repo_path","label":"Repo/path"}]},
          {"key":"from_workflow_id","label":"Source workflow ID","type":"text","defaultValue":""},
          {"key":"repo_path","label":"Repo/path","type":"text","defaultValue":""},
          {"key":"rtl_text","label":"RTL text","type":"text","defaultValue":""},
          {"key":"spec_text","label":"Spec or review intent","type":"text","defaultValue":""},
          {"key":"review_depth","label":"Review depth","type":"select","defaultValue":"standard","options":["quick","standard","deep"]}
        ]
      }'::jsonb
    ),
    (
      'Digital_Constraint_Review',
      '{
        "note":"Review timing constraints before synthesis, STA, or tapeout.",
        "fields":[
          {"key":"rtl_source_mode","label":"RTL source","type":"select","defaultValue":"from_arch2rtl","options":[{"value":"from_arch2rtl","label":"Prior workflow"},{"value":"paste","label":"Paste RTL"},{"value":"repo_path","label":"Repo/path"}]},
          {"key":"from_workflow_id","label":"Source workflow ID","type":"text","defaultValue":""},
          {"key":"repo_path","label":"Repo/path","type":"text","defaultValue":""},
          {"key":"rtl_text","label":"RTL text","type":"text","defaultValue":""},
          {"key":"constraints_sdc","label":"Constraints SDC","type":"text","defaultValue":""},
          {"key":"target_frequency_mhz","label":"Target frequency MHz","type":"number","defaultValue":100}
        ]
      }'::jsonb
    ),
    (
      'Digital_Timing_Debug',
      '{
        "note":"Debug timing reports from synthesis or implementation runs.",
        "fields":[
          {"key":"source_workflow_id","label":"Source workflow ID","type":"text","defaultValue":""},
          {"key":"timing_report_text","label":"Timing report text","type":"text","defaultValue":""},
          {"key":"stage","label":"Timing stage","type":"select","defaultValue":"auto","options":["auto","synthesis","preplace","postplace","postcts","postroute","postfill"]},
          {"key":"target_frequency_mhz","label":"Target frequency MHz","type":"number","defaultValue":100}
        ]
      }'::jsonb
    )
)
insert into public.product_stage_schemas (app, schema, is_active, updated_at)
select app, schema, true, now()
from schemas
on conflict (app) do update
set schema = excluded.schema,
    is_active = true,
    updated_at = now();
