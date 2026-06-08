-- Register Spec2RTL conformance agent and standalone workflow.
-- Safe to re-run.

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
    'Digital Spec2RTL Conformance Agent'::text as agent_name,
    'Digital Spec2RTL Conformance Agent'::text as name,
    'digital'::text as loop_type,
    'digital'::text as domain,
    'Checks generated or imported RTL against a claimed specification, including top/module evidence, interfaces, register intent, reset/clock evidence, and requirement trace with matched, partial, missing, and inconclusive outcomes.'::text as description,
    'agents.digital.digital_spec2rtl_conformance_agent:run_agent'::text as script_path,
    'agents.digital.digital_spec2rtl_conformance_agent:run_agent'::text as entrypoint,
    'native'::text as execution_mode,
    '["spec_text", "spec_json", "rtl/*.v", "rtl/*.sv", "handoff/rtl/*.v", "handoff/rtl/*.sv", "digital_regmap.json"]'::jsonb as inputs,
    '["digital/spec2rtl/spec2rtl_conformance.json", "digital/spec2rtl/SPEC2RTL_CONFORMANCE.md"]'::jsonb as outputs,
    '["digital/spec2rtl/spec2rtl_conformance.json", "digital/spec2rtl/SPEC2RTL_CONFORMANCE.md"]'::jsonb as artifact_paths,
    '["structured_data", "report"]'::jsonb as artifact_types,
    '["rtl_static_analysis", "requirements_traceability", "artifact_publish"]'::jsonb as required_skills,
    '["python", "supabase"]'::jsonb as required_tools,
    '["pre_run_validate_inputs", "post_run_collect_artifacts", "post_run_update_state", "on_failure_capture_traceback", "on_failure_preserve_logs", "artifact_publish_to_supabase"]'::jsonb as hooks,
    '{"registry_source": "DIGITAL_AGENT_FUNCTIONS", "variable": "digital_spec2rtl_conformance_agent", "studio_contract_version": "v1"}'::jsonb as metadata,
    '{"name": "Digital Spec2RTL Conformance Agent", "loop_type": "digital", "domain": "digital", "entrypoint": "agents.digital.digital_spec2rtl_conformance_agent:run_agent", "execution_mode": "native"}'::jsonb as agent_spec
),
updated_agent as (
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
  from agent_template t
  where (a.agent_name = t.agent_name or a.name = t.name)
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
  t.agent_name, t.name, t.loop_type, t.domain, t.description, t.script_path, t.entrypoint, t.execution_mode,
  t.inputs, t.outputs, t.artifact_paths, t.artifact_types, t.required_skills, t.required_tools,
  t.agent_spec, t.required_skills, t.required_tools, t.hooks, t.metadata, null, false, true,
  false, 'approved', 'global', 'platform_registry', now(), now()
from agent_template t
where not exists (
  select 1 from public.agents a
  where (a.agent_name = t.agent_name or a.name = t.name)
    and a.owner_id is null
);

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with template(name, loop_type, agents) as (
  values (
    'Digital_Spec2RTL_Check',
    'digital',
    array[
      'Digital RTL Handoff Ingest Agent',
      'Digital Spec2RTL Conformance Agent'
    ]::text[]
  )
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
            'position', jsonb_build_object('x', 80 + ((ord - 1) * 372), 'y', 160),
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
            jsonb_build_object('id', 'e' || ord, 'source', 'n' || ord, 'target', 'n' || (ord + 1))
            order by ord
          )
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      )
    ) as definitions
  from template t
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
  gen_random_uuid(), null, d.name, d.loop_type, d.definitions, d.definitions->'nodes',
  d.definitions->'edges', 'saved', true, now(), now()
from definitions d
where not exists (
  select 1
  from public.workflows w
  where w.name = d.name
    and w.user_id is null
);
