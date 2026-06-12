-- Reassert System PD analog physical generation as Supabase source of truth.
-- This migration updates the public.agents rows for the analog physical agents
-- and the public.workflows prebuilt System_PD template. The agents generate
--/consume real SPICE/GDS/LEF/LIB collateral and fail when required tools or
-- artifacts are missing.

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
  select *
  from (
    values
      (
        'Analog Sky130 SPICE Netlist Agent',
        'analog',
        'analog',
        'Generates or materializes real transistor-level Sky130 SPICE for analog GDS generation. It validates real MOS devices and fails when no valid device-level SPICE is available.',
        'agents.analog.analog_sky130_spice_netlist_agent:run_agent',
        'native',
        array['analog/spec.json','analog_macro_interface_contract','analog_spice_text(optional)','analog_spice_path(optional)']::text[],
        array['analog/sky130/<macro>.spice','analog/sky130/sky130_spice_summary.json']::text[],
        array['analog/sky130/<macro>.spice','analog/sky130/sky130_spice_summary.json']::text[],
        array['implementation_artifact','structured_data']::text[],
        array['analog_generation','artifact_publish']::text[],
        array['openai_or_portkey_llm','python','supabase']::text[],
        'analog_sky130_spice_netlist_agent'
      ),
      (
        'Analog GDS Generation Agent',
        'analog',
        'analog',
        'Runs ALIGN/schematic2layout against generated or provided Sky130 transistor-level SPICE and fails when ALIGN is unavailable or no GDS is produced.',
        'agents.analog.analog_gds_generation_agent:run_agent',
        'native',
        array['analog/sky130/<macro>.spice','analog_spice_path']::text[],
        array['analog/gds/<macro>.gds','analog/gds/analog_gds_summary.json','analog/gds/run_align.sh']::text[],
        array['analog/gds/<macro>.gds','analog/gds/analog_gds_summary.json','analog/gds/run_align.sh']::text[],
        array['layout','structured_data','script']::text[],
        array['analog_layout_generation','artifact_publish']::text[],
        array['align','schematic2layout.py','python','supabase']::text[],
        'analog_gds_generation_agent'
      ),
      (
        'Analog LEF Extraction Agent',
        'analog',
        'analog',
        'Extracts analog macro LEF from generated GDS using Magic for downstream OpenLane macro integration.',
        'agents.analog.analog_lef_extraction_agent:run_agent',
        'native',
        array['analog_macro_gds','analog/gds/<macro>.gds']::text[],
        array['analog/lef_extract/<macro>.lef','analog/lef_extract/lef_extract_summary.json','analog/lef_extract/extract_lef.tcl']::text[],
        array['analog/lef_extract/<macro>.lef','analog/lef_extract/lef_extract_summary.json','analog/lef_extract/extract_lef.tcl']::text[],
        array['implementation_artifact','structured_data','script']::text[],
        array['lef_extraction','artifact_publish']::text[],
        array['magic','python','supabase']::text[],
        'analog_lef_extraction_agent'
      ),
      (
        'Analog Liberty Characterization Agent',
        'analog',
        'analog',
        'Runs ngspice characterization setup for generated Sky130 SPICE and records measured characterization evidence for Liberty handoff.',
        'agents.analog.analog_liberty_characterization_agent:run_agent',
        'native',
        array['analog_spice_path','analog/sky130/<macro>.spice']::text[],
        array['analog/lib_char/liberty_characterization_summary.json','analog/lib_char/characterize_ngspice.sp','analog/lib_char/ngspice_characterization.log']::text[],
        array['analog/lib_char/liberty_characterization_summary.json','analog/lib_char/characterize_ngspice.sp','analog/lib_char/ngspice_characterization.log']::text[],
        array['structured_data','report','script']::text[],
        array['liberty_characterization','artifact_publish']::text[],
        array['ngspice','python','supabase']::text[],
        'analog_liberty_characterization_agent'
      ),
      (
        'Analog Collateral Consistency Agent',
        'analog',
        'analog',
        'Checks analog macro SPICE/GDS/LEF/LIB collateral consistency before physical digital integration.',
        'agents.analog.analog_collateral_consistency_agent:run_agent',
        'native',
        array['analog_spice_path','analog_macro_gds','analog_macro_lef','analog_macro_lib','analog_macro_interface_contract']::text[],
        array['analog/consistency/analog_collateral_consistency.json']::text[],
        array['analog/consistency/analog_collateral_consistency.json']::text[],
        array['structured_data','report']::text[],
        array['collateral_consistency','artifact_publish']::text[],
        array['python','supabase']::text[],
        'analog_collateral_consistency_agent'
      ),
      (
        'Analog Physical Collateral Package Agent',
        'analog',
        'analog',
        'Packages generated analog physical collateral for digital PD macro LEF/LIB/GDS handoff.',
        'agents.analog.analog_physical_collateral_package_agent:run_agent',
        'native',
        array['analog_spice_path','analog_macro_gds','analog_macro_lef','analog_macro_lib','analog_collateral_consistency']::text[],
        array['analog/physical_package/analog_physical_package.json']::text[],
        array['analog/physical_package/analog_physical_package.json']::text[],
        array['handoff','structured_data']::text[],
        array['physical_collateral_package','artifact_publish']::text[],
        array['python','supabase']::text[],
        'analog_physical_collateral_package_agent'
      )
  ) as v(agent_name, loop_type, domain, description, entrypoint, execution_mode, inputs, outputs, artifact_paths, artifact_types, required_skills, required_tools, variable_name)
),
normalized_agent as (
  select
    agent_name,
    agent_name as name,
    loop_type,
    domain,
    description,
    entrypoint as script_path,
    entrypoint,
    execution_mode,
    to_jsonb(inputs) as inputs,
    to_jsonb(outputs) as outputs,
    to_jsonb(artifact_paths) as artifact_paths,
    to_jsonb(artifact_types) as artifact_types,
    to_jsonb(required_skills) as required_skills,
    to_jsonb(required_tools) as required_tools,
    to_jsonb(array[
      'pre_run_validate_inputs',
      'post_run_collect_artifacts',
      'post_run_update_state',
      'on_failure_capture_traceback',
      'on_failure_preserve_logs',
      'artifact_publish_to_supabase'
    ]::text[]) as hooks,
    jsonb_build_object(
      'registry_source', 'ANALOG_AGENT_FUNCTIONS',
      'variable', variable_name,
      'studio_contract_version', 'v1',
      'format', 'agents_skills_tools_hooks'
    ) as metadata,
    jsonb_build_object(
      'name', agent_name,
      'loop_type', loop_type,
      'domain', domain,
      'entrypoint', entrypoint,
      'execution_mode', execution_mode,
      'skills', to_jsonb(required_skills),
      'tools', to_jsonb(required_tools),
      'hooks', to_jsonb(array[
        'pre_run_validate_inputs',
        'post_run_collect_artifacts',
        'post_run_update_state',
        'on_failure_capture_traceback',
        'on_failure_preserve_logs',
        'artifact_publish_to_supabase'
      ]::text[])
    ) as agent_spec
  from agent_template
),
updated_agent as (
  update public.agents as agent
  set agent_name = normalized_agent.agent_name,
      name = normalized_agent.name,
      loop_type = normalized_agent.loop_type,
      domain = normalized_agent.domain,
      description = normalized_agent.description,
      script_path = normalized_agent.script_path,
      entrypoint = normalized_agent.entrypoint,
      execution_mode = normalized_agent.execution_mode,
      inputs = normalized_agent.inputs,
      outputs = normalized_agent.outputs,
      artifact_paths = normalized_agent.artifact_paths,
      artifact_types = normalized_agent.artifact_types,
      required_skills = normalized_agent.required_skills,
      required_tools = normalized_agent.required_tools,
      agent_spec = normalized_agent.agent_spec,
      skills = normalized_agent.required_skills,
      tools = normalized_agent.required_tools,
      hooks = normalized_agent.hooks,
      metadata = normalized_agent.metadata,
      owner_id = null,
      is_custom = false,
      is_prebuilt = true,
      is_marketplace = false,
      status = 'approved',
      visibility = 'global',
      source = 'platform_registry',
      updated_at = now()
  from normalized_agent
  where (agent.agent_name = normalized_agent.agent_name or agent.name = normalized_agent.name)
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
from normalized_agent
where not exists (
  select 1
  from public.agents as agent
  where (agent.agent_name = normalized_agent.agent_name or agent.name = normalized_agent.name)
    and agent.owner_id is null
);

with template(name, loop_type, agents) as (
  values
    ('System_PD', 'system', array[
      'Digital Spec Agent',
      'Digital Architecture Agent',
      'Digital Microarchitecture Agent',
      'Digital Register Map Agent',
      'Digital RTL Agent',
      'Digital RTL Linting Agent',
      'Digital RTL Signature Agent',
      'Analog Spec Builder Agent',
      'Analog Behavioral Model Agent',
      'Analog Abstract Views Agent',
      'System Integration Intent Agent',
      'System Top Assembly Agent',
      'Analog Macro Interface Contract Agent',
      'System Assertions (SVA) Agent',
      'System RTL Handoff Package Agent',
      'System RTL Evidence Dashboard Agent',
      'Analog Macro Interface Validation Agent',
      'Digital Foundry Profile Agent',
      'Digital Implementation Setup Agent',
      'Digital Synthesis Agent',
      'Digital Logic Equivalence Check Agent',
      'Digital DFT Scan Stitching Agent',
      'Digital Scan ATPG Coverage Agent',
      'Digital MBIST Collateral Agent',
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
      'Digital Fill Agent',
      'Digital DRC Agent',
      'Digital LVS Agent',
      'Digital Tapeout Agent',
      'Digital Tapeout Logic Equivalence Check Agent',
      'Digital Executive Summary Agent'
    ]::text[])
),
defs as (
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
            'position', jsonb_build_object(
              'x', 80 + ((ord - 1) % 4) * 372,
              'y', 160 + floor((ord - 1) / 4) * 210
            ),
            'data', jsonb_build_object(
              'desc', 'Auto-composed: ' || agent,
              'uiLabel', agent,
              'backendLabel', agent
            )
          )
          order by ord
        )
        from unnest(agents) with ordinality as u(agent, ord)
      ),
      'edges',
      (
        select coalesce(
          jsonb_agg(
            jsonb_build_object(
              'id', 'e' || ord,
              'source', 'n' || ord,
              'target', 'n' || (ord + 1)
            )
            order by ord
          ),
          '[]'::jsonb
        )
        from generate_series(1, greatest(array_length(agents, 1) - 1, 0)) as ord
      )
    ) as definitions
  from template
)
update public.workflows as workflow
set
  loop_type = defs.loop_type,
  definitions = defs.definitions,
  nodes = defs.definitions->'nodes',
  edges = defs.definitions->'edges',
  status = 'saved',
  is_prebuilt = true,
  updated_at = now()
from defs
where workflow.name = defs.name
  and coalesce(workflow.is_prebuilt, false) = true
  and workflow.user_id is null;
