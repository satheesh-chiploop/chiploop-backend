-- Align System RTL/System Sim platform templates with current source-of-truth runtime.
-- Safe to re-run. Avoids ON CONFLICT because existing deployments may not have
-- a unique constraint on public.agents/public.workflows names.

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

with agent_template(agent_name, description, entrypoint, execution_mode, inputs, outputs, artifact_paths, artifact_types, required_skills, required_tools, variable_name) as (
  values
    (
      'System CoSim Ingest Agent',
      'Restores and normalizes Software, Firmware, and RTL packages into a single System CoSim manifest for system simulation and full-stack validation.',
      'agents.system.system_cosim_ingest_agent:run_agent',
      'legacy_adapter',
      array['system/software/package/system_software_package.json(optional)','system/software_validation/summary/system_software_validation_summary.json(optional)','firmware/firmware_manifest.json(optional)','firmware/build/firmware.elf(optional)','firmware/register_map.json(optional)','system/package/system_rtl_package.json(optional)','system/integration/soc_top_sim.sv(optional)','system/integration/system_rtl_filelist_sim.txt(optional)']::text[],
      array['system/cosim/ingest/system_cosim_manifest.json','system/cosim/ingest/system_cosim_manifest.md','system/cosim/ingest/system_cosim_ingest_debug.json']::text[],
      array['system/cosim/ingest/system_cosim_manifest.json','system/cosim/ingest/system_cosim_manifest.md','system/cosim/ingest/system_cosim_ingest_debug.json']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish','spec_ingest','supabase_handoff_ingest']::text[],
      array['iverilog','openai_or_portkey_llm','python','supabase','verilator']::text[],
      'system_cosim_ingest_agent'
    ),
    (
      'System Functional Coverage Agent',
      'Generates lightweight system-level functional coverage from integration intent, observable behaviors, ownership rules, and optional software-visible register/control metadata.',
      'agents.system.system_functional_coverage_agent:run_agent',
      'legacy_adapter',
      array['system/integration/system_integration_intent.json','system/integration/soc_top_sim.sv','system/integration/system_rtl_filelist_sim.txt','digital/digital_regmap.json(optional)','*.v','*.sv']::text[],
      array['vv/tb/coverage_model.py','vv/tb/COVERAGE.md','vv/tb/coverage_generation_report.json','functional_coverage_agent.log']::text[],
      array['vv/tb/coverage_model.py','vv/tb/COVERAGE.md','vv/tb/coverage_generation_report.json','functional_coverage_agent.log']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish']::text[],
      array['python','supabase']::text[],
      'system_functional_coverage_agent'
    ),
    (
      'System Testbench Generator Agent',
      'Generates system-level Cocotb testbench collateral from system integration intent, assembled SoC top, system RTL filelist, and optional digital register-map/control metadata.',
      'agents.system.system_testbench_generator_agent:run_agent',
      'legacy_adapter',
      array['system/integration/system_integration_intent.json','system/integration/soc_top_sim.sv','system/integration/system_rtl_filelist_sim.txt','digital/digital_regmap.json(optional)','handoff/MANIFEST.json(optional)','*.v','*.sv']::text[],
      array['vv/tb/test_*.py','vv/tb/Makefile','vv/tb/README.md','vv/tb/tb_generation_report.json','vv/testbench_generator_agent.log']::text[],
      array['vv/tb/test_*.py','vv/tb/Makefile','vv/tb/README.md','vv/tb/tb_generation_report.json','vv/testbench_generator_agent.log']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish','cocotb_testbench_generation']::text[],
      array['cocotb','iverilog','python','supabase','verilator']::text[],
      'system_testbench_generator_agent'
    ),
    (
      'System Simulation Control Agent',
      'Generates system-level regression orchestration and simulation manifest for Cocotb/Verilator runs using assembled SoC top, system RTL filelist, generated testbench collateral, and optional assertions/coverage artifacts.',
      'agents.system.system_simulation_control_agent:run_agent',
      'legacy_adapter',
      array['system/integration/system_integration_intent.json','system/integration/soc_top_sim.sv','system/integration/system_rtl_filelist_sim.txt','vv/tb/Makefile','vv/tb/test_*.py','assertions.sv(optional)','vv/tb/coverage_model.py(optional)','*.v','*.sv']::text[],
      array['vv/tb/run_regression.py','vv/tb/SIM_CONTROL.md','vv/tb/simulation_manifest.json','vv/tb/sim_control_generation_report.json','simulation_control_agent.log']::text[],
      array['vv/tb/run_regression.py','vv/tb/SIM_CONTROL.md','vv/tb/simulation_manifest.json','vv/tb/sim_control_generation_report.json','simulation_control_agent.log']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish']::text[],
      array['iverilog','python','supabase','verilator']::text[],
      'system_simulation_control_agent'
    ),
    (
      'System Simulation Execution Agent',
      'Runs System_SIM execution using generated Cocotb/Verilator collateral and captures pass/fail, runtime, waveforms, and raw coverage candidates.',
      'agents.system.system_sim_execution_agent:run_agent',
      'legacy_adapter',
      array['system/integration/soc_top_sim.sv','vv/tb/Makefile','vv/tb/test_*.py','vv/tb/coverage_model.py(optional)','vv/tb/run_regression.py(optional)','*.v','*.sv']::text[],
      array['system/sim/system_sim_execution.json','system/sim/system_sim_execution.md','system/sim/logs/*.log']::text[],
      array['system/sim/system_sim_execution.json','system/sim/system_sim_execution.md','system/sim/logs/*.log']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish','rtl_compile_check']::text[],
      array['iverilog','python','supabase','verilator']::text[],
      'system_sim_execution_agent'
    ),
    (
      'System Simulation Coverage Summary Agent',
      'Parses System_SIM execution artifacts and publishes functional/code/assertion coverage plus run summary for UI display.',
      'agents.system.system_sim_coverage_summary_agent:run_agent',
      'legacy_adapter',
      array['system/sim/system_sim_execution.json','vv/tb/coverage_model.py(optional)','vv/tb/COVERAGE.md(optional)','assertions.sv(optional)','*.log(optional)']::text[],
      array['system/sim/system_sim_dashboard.json','system/sim/system_sim_dashboard.md']::text[],
      array['system/sim/system_sim_dashboard.json','system/sim/system_sim_dashboard.md']::text[],
      array['report','structured_data']::text[],
      array['artifact_publish']::text[],
      array['iverilog','python','supabase','verilator']::text[],
      'system_sim_coverage_summary_agent'
    )
),
normalized_agent_template as (
  select
    agent_name,
    agent_name as name,
    'system'::text as loop_type,
    'system'::text as domain,
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
      'registry_source', 'SYSTEM_AGENT_FUNCTIONS',
      'variable', variable_name,
      'studio_contract_version', 'v1'
    ) as metadata,
    jsonb_build_object(
      'name', agent_name,
      'loop_type', 'system',
      'domain', 'system',
      'entrypoint', entrypoint,
      'execution_mode', execution_mode
    ) as agent_spec
  from agent_template
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
  from normalized_agent_template as agent_template
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
from normalized_agent_template as agent_template
where not exists (
  select 1
  from public.agents as agent
  where (agent.agent_name = agent_template.agent_name or agent.name = agent_template.name)
    and agent.owner_id is null
);

with template(name, loop_type, agents) as (
  values
    (
      'System_RTL',
      'system',
      array[
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
        'Digital Spec2RTL Conformance Agent',
        'System RTL Evidence Dashboard Agent'
      ]::text[]
    ),
    (
      'System_Sim',
      'system',
      array[
        'System CoSim Ingest Agent',
        'System Testbench Generator Agent',
        'System Functional Coverage Agent',
        'System Simulation Control Agent',
        'System Simulation Execution Agent',
        'System Simulation Coverage Summary Agent'
      ]::text[]
    ),
    (
      'System_Firmware',
      'system',
      array[
        'Embedded Firmware Register Extract Agent',
        'Embedded Rust Register Layer Generator Agent',
        'Embedded Register Validation Agent',
        'Embedded Rust Driver Scaffold Agent',
        'Embedded Interrupt Mapping Agent',
        'Embedded Firmware Integration Contract Agent',
        'Embedded ELF Build Agent',
        'Embedded Verilator Build Agent',
        'Embedded Cocotb Harness Agent',
        'Embedded Co Sim Runner Agent',
        'System Firmware CoSim Execution Agent',
        'System Firmware Coverage Summary Agent',
        'Embedded Coverage Collector Agent',
        'Embedded Validation Report Agent',
        'Embedded Firmware Executive Summary Agent',
        'System Software Handoff Package Agent'
      ]::text[]
    )
),
definitions as (
  select
    template.name,
    template.loop_type,
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
              'desc', 'Auto-composed: ' || agent_name,
              'uiLabel', agent_name,
              'backendLabel', agent_name
            )
          )
          order by ord
        )
        from unnest(template.agents) with ordinality as agent(agent_name, ord)
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
          from generate_series(1, greatest(array_length(template.agents, 1) - 1, 0)) as ord
        ),
        '[]'::jsonb
      )
    ) as definitions
  from template
),
updated_workflow as (
  update public.workflows as workflow
  set definitions = definitions.definitions,
      nodes = definitions.definitions->'nodes',
      edges = definitions.definitions->'edges',
      loop_type = definitions.loop_type,
      is_prebuilt = true,
      user_id = null,
      status = 'saved',
      updated_at = now()
  from definitions
  where workflow.name = definitions.name
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
  definitions.name,
  definitions.loop_type,
  definitions.definitions,
  definitions.definitions->'nodes',
  definitions.definitions->'edges',
  'saved',
  true,
  now(),
  now()
from definitions
where not exists (
  select 1
  from public.workflows as workflow
  where workflow.name = definitions.name
    and workflow.user_id is null
);

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;
