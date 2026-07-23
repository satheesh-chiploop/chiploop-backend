-- Add FPGA Loop for iCE40 open-source prototyping.
-- Supabase remains the source of truth for prebuilt agents and workflows.

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

alter table if exists public.agents
  drop constraint if exists agents_loop_type_chk;

alter table if exists public.agents
  add constraint agents_loop_type_chk
  check (
    loop_type is null
    or loop_type in ('digital', 'analog', 'system', 'embedded', 'validation', 'fpga')
  );

alter table if exists public.workflows
  drop constraint if exists workflows_loop_type_chk;

alter table if exists public.workflows
  add constraint workflows_loop_type_chk
  check (
    loop_type is null
    or loop_type in ('digital', 'analog', 'system', 'embedded', 'validation', 'fpga')
  );

with agent_templates(agent_name, description, entrypoint, inputs, outputs, required_tools) as (
  values
    (
      'FPGA RTL Handoff Ingest Agent',
      'Collects RTL from a prior workflow, pasted source, or repo path and prepares an FPGA implementation handoff.',
      'agents.fpga.fpga_rtl_handoff_ingest_agent:run_agent',
      '["from_workflow_id","source_workflow_id","pasted_rtl_files","rtl_text","repo_path","repo_subdir","top_module","board","family"]'::jsonb,
      '["fpga/handoff/fpga_handoff_ingest.json"]'::jsonb,
      '["python"]'::jsonb
    ),
    (
      'FPGA Constraint Setup Agent',
      'Selects the FPGA target board/device and prepares PCF constraints for open-source iCE40 implementation.',
      'agents.fpga.fpga_constraint_setup_agent:run_agent',
      '["board","device","package","constraints_pcf","pcf_text","pcf_path","target_frequency_mhz"]'::jsonb,
      '["fpga/constraints/fpga_constraints_summary.json","fpga/constraints/top.pcf"]'::jsonb,
      '["python"]'::jsonb
    ),
    (
      'FPGA Yosys Synthesis Agent',
      'Runs Yosys synthesis for iCE40 and emits a JSON netlist for nextpnr.',
      'agents.fpga.fpga_yosys_synthesis_agent:run_agent',
      '["top_module","rtl_files","device","family"]'::jsonb,
      '["fpga/synth/fpga_synthesis_summary.json","fpga/synth/*.json","fpga/synth/synth_ice40.ys"]'::jsonb,
      '["yosys"]'::jsonb
    ),
    (
      'FPGA nextpnr Place & Route Agent',
      'Runs nextpnr-ice40 place-and-route and collects timing, utilization, and routing evidence.',
      'agents.fpga.fpga_nextpnr_place_route_agent:run_agent',
      '["board","device","package","pcf","netlist_json","target_frequency_mhz"]'::jsonb,
      '["fpga/pnr/fpga_place_route_summary.json","fpga/pnr/*.asc","fpga/pnr/*.log"]'::jsonb,
      '["nextpnr-ice40"]'::jsonb
    ),
    (
      'FPGA Timing & DRC Agent',
      'Runs iCE40 timing checks and summarizes timing/DRC readiness before bitstream generation.',
      'agents.fpga.fpga_timing_drc_agent:run_agent',
      '["asc","device","package","target_frequency_mhz"]'::jsonb,
      '["fpga/reports/fpga_timing_drc_summary.json","fpga/reports/*.log"]'::jsonb,
      '["icetime"]'::jsonb
    ),
    (
      'FPGA Bitstream Handoff Agent',
      'Runs IceStorm bitstream packaging and produces board programming handoff instructions.',
      'agents.fpga.fpga_bitstream_handoff_agent:run_agent',
      '["asc","board","generate_bitstream"]'::jsonb,
      '["fpga/bitstream/fpga_bitstream_summary.json","fpga/bitstream/*.bin"]'::jsonb,
      '["icepack","openFPGALoader"]'::jsonb
    ),
    (
      'FPGA Dashboard Agent',
      'Builds the FPGA dashboard summary from handoff, constraints, synthesis, place-and-route, timing, and bitstream artifacts.',
      'agents.fpga.fpga_dashboard_agent:run_agent',
      '["workflow_id","fpga"]'::jsonb,
      '["fpga/fpga_dashboard.json"]'::jsonb,
      '["python"]'::jsonb
    )
),
normalized_agents as (
  select
    agent_name,
    agent_name as name,
    'fpga'::text as loop_type,
    'fpga'::text as domain,
    description,
    entrypoint as script_path,
    entrypoint,
    'native'::text as execution_mode,
    inputs,
    outputs,
    outputs as artifact_paths,
    '["structured_data","report","implementation_artifact"]'::jsonb as artifact_types,
    '["fpga_implementation","artifact_publish"]'::jsonb as required_skills,
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
      'registry_source', 'FPGA_AGENT_FUNCTIONS',
      'studio_contract_version', 'v1',
      'vendor', 'lattice',
      'family', 'ice40'
    ) as metadata,
    jsonb_build_object(
      'name', agent_name,
      'loop_type', 'fpga',
      'domain', 'fpga',
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
    (
      'FPGA_RTL_to_Bitstream',
      'Runs an iCE40 FPGA prototype flow from RTL handoff through constraints, Yosys synthesis, nextpnr place-and-route, timing/DRC checks, and bitstream handoff.',
      array[
        'FPGA RTL Handoff Ingest Agent',
        'FPGA Constraint Setup Agent',
        'FPGA Yosys Synthesis Agent',
        'FPGA nextpnr Place & Route Agent',
        'FPGA Timing & DRC Agent',
        'FPGA Bitstream Handoff Agent',
        'FPGA Dashboard Agent'
      ]::text[]
    )
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
            'type', 'agent',
            'position', jsonb_build_object('x', 80 + ((ord - 1) * 240), 'y', 180),
            'data', jsonb_build_object(
              'uiLabel', replace(agent_name, 'FPGA ', ''),
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
          select jsonb_agg(jsonb_build_object('id', 'e' || ord, 'source', 'n' || ord, 'target', 'n' || (ord + 1)) order by ord)
          from generate_series(1, greatest(array_length(t.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      ),
      'description',
      t.description,
      'input_contract',
      jsonb_build_object(
        'fields',
        jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path')),
          jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
          jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
          jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','upduino_v3','icestick','custom_ice40')),
          jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
          jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
          jsonb_build_object('key','pcf_text','label','Pin constraints PCF','type','textarea','required',false)
        )
      )
    ) as definitions
  from templates t
),
updated_workflows as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = 'fpga',
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
  'fpga',
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
