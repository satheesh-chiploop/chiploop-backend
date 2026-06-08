-- Register System RTL Evidence Dashboard Agent and wire it into System RTL/Synthesis/PD templates.
-- Safe to re-run. Avoids ON CONFLICT because older deployments may not have unique constraints.

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
    'Builds an integrated System RTL evidence dashboard from real RTL filelists, separating digital RTL, analog macro RTL, and SoC top RTL into Arch2RTL-style interface, storage, timing, module, and file-count metrics.'::text as description,
    'agents.system.system_rtl_dashboard_agent:run_agent'::text as script_path,
    'agents.system.system_rtl_dashboard_agent:run_agent'::text as entrypoint,
    'native'::text as execution_mode,
    '[
      "system/package/system_rtl_package.json",
      "system/integration/system_rtl_filelist_sim.txt",
      "system/integration/system_rtl_filelist_phys.txt",
      "system/integration/system_full_compile_summary.json"
    ]'::jsonb as inputs,
    '[
      "system/dashboard/system_rtl_dashboard.json",
      "system/dashboard/SYSTEM_RTL_DASHBOARD.md"
    ]'::jsonb as outputs,
    '[
      "system/dashboard/system_rtl_dashboard.json",
      "system/dashboard/SYSTEM_RTL_DASHBOARD.md"
    ]'::jsonb as artifact_paths,
    '[
      "structured_data",
      "report"
    ]'::jsonb as artifact_types,
    '[
      "rtl_analysis",
      "artifact_publish",
      "dashboard_evidence"
    ]'::jsonb as required_skills,
    '[
      "python",
      "supabase"
    ]'::jsonb as required_tools,
    '[
      "pre_run_validate_inputs",
      "post_run_collect_artifacts",
      "post_run_update_state",
      "on_failure_capture_traceback",
      "on_failure_preserve_logs",
      "artifact_publish_to_supabase"
    ]'::jsonb as hooks,
    '{
      "registry_source": "SYSTEM_AGENT_FUNCTIONS",
      "variable": "system_rtl_dashboard_agent",
      "studio_contract_version": "v1"
    }'::jsonb as metadata,
    '{
      "name": "System RTL Evidence Dashboard Agent",
      "loop_type": "system",
      "domain": "system",
      "entrypoint": "agents.system.system_rtl_dashboard_agent:run_agent",
      "execution_mode": "native"
    }'::jsonb as agent_spec
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
  agent_name, name, loop_type, domain, description, script_path, entrypoint,
  execution_mode, inputs, outputs, artifact_paths, artifact_types, required_skills,
  required_tools, agent_spec, skills, tools, hooks, metadata, owner_id, is_custom,
  is_prebuilt, is_marketplace, status, visibility, source, created_at, updated_at
)
select
  t.agent_name, t.name, t.loop_type, t.domain, t.description, t.script_path, t.entrypoint,
  t.execution_mode, t.inputs, t.outputs, t.artifact_paths, t.artifact_types, t.required_skills,
  t.required_tools, t.agent_spec, t.required_skills, t.required_tools, t.hooks, t.metadata,
  null, false, true, false, 'approved', 'global', 'platform_registry', now(), now()
from agent_template t
where not exists (
  select 1
  from public.agents a
  where (a.agent_name = t.agent_name or a.name = t.name)
    and a.owner_id is null
);

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with templates(name, loop_type, agents) as (
  values
    (
      'System_RTL',
      'system',
      array[
        'System Integration Intent Agent',
        'System Top Assembly Agent',
        'System Assertions (SVA) Agent',
        'System RTL Handoff Package Agent',
        'System RTL Evidence Dashboard Agent'
      ]::text[]
    ),
    (
      'System_Synthesis',
      'system',
      array[
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
      ]::text[]
    ),
    (
      'System_PD',
      'system',
      array[
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
