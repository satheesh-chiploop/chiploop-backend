-- Add bounded synthesis closure loops for Arch2Synthesis, System Synthesis, and System PD.
-- This migration avoids ON CONFLICT because some deployments lack unique constraints.

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
  add column if not exists updated_at timestamptz default now();

alter table if exists public.workflows
  add column if not exists definitions jsonb,
  add column if not exists nodes jsonb,
  add column if not exists edges jsonb,
  add column if not exists loop_type text,
  add column if not exists status text,
  add column if not exists is_prebuilt boolean not null default false,
  add column if not exists updated_at timestamptz default now();

alter table if exists public.product_stage_schemas
  add column if not exists app text,
  add column if not exists schema jsonb,
  add column if not exists is_active boolean not null default true,
  add column if not exists updated_at timestamptz default now();

with agent_template as (
  select * from (
    values
      ('Digital Synthesis Closure Agent','digital','digital','Analyzes synthesis/pre-place setup timing and synthesis LEC evidence. Produces a bounded closure restart plan without faking repair success.','agents.digital.digital_synthesis_closure_agent:run_agent'),
      ('System Synthesis Closure Agent','system','digital','Analyzes System Synthesis setup timing and synthesis LEC evidence. Produces a bounded closure restart plan without faking repair success.','agents.system.system_synthesis_closure_agent:run_agent')
  ) as t(name, loop_type, domain, description, entrypoint)
),
updated_agents as (
  update public.agents agent
  set agent_name = agent_template.name,
      name = agent_template.name,
      loop_type = agent_template.loop_type,
      domain = agent_template.domain,
      description = agent_template.description,
      script_path = agent_template.entrypoint,
      entrypoint = agent_template.entrypoint,
      execution_mode = 'legacy_adapter',
      inputs = '["digital/synth/metrics.json","digital/synth/synth_summary.json","digital/sta_preplace/metrics.json","digital/lec/lec_summary.json"]'::jsonb,
      outputs = '["digital/synthesis_closure/synthesis_closure_plan.json","digital/synthesis_closure/synthesis_closure_chart.json","digital/synthesis_closure/synthesis_closure_plan.md"]'::jsonb,
      artifact_paths = '["digital/synthesis_closure/synthesis_closure_plan.json","digital/synthesis_closure/synthesis_closure_chart.json","digital/synthesis_closure/synthesis_closure_plan.md"]'::jsonb,
      artifact_types = '["report","structured_data"]'::jsonb,
      required_skills = '["artifact_publish","synthesis_timing_triage"]'::jsonb,
      required_tools = '["python","supabase"]'::jsonb,
      skills = '["artifact_publish","synthesis_timing_triage"]'::jsonb,
      tools = '["python","supabase"]'::jsonb,
      hooks = '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
      metadata = jsonb_build_object('registry_source','SYNTHESIS_CLOSURE_MIGRATION','studio_contract_version','v1'),
      is_custom = false,
      is_prebuilt = true,
      is_marketplace = false,
      status = 'approved',
      visibility = 'global',
      source = 'platform_registry',
      updated_at = now()
  from agent_template
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
  returning agent.name
)
insert into public.agents (
  agent_name, name, loop_type, domain, description, script_path, entrypoint,
  execution_mode, inputs, outputs, artifact_paths, artifact_types,
  required_skills, required_tools, skills, tools, hooks, metadata,
  is_custom, is_prebuilt, is_marketplace, status, visibility, source, updated_at
)
select
  name, name, loop_type, domain, description, entrypoint, entrypoint,
  'legacy_adapter',
  '["digital/synth/metrics.json","digital/synth/synth_summary.json","digital/sta_preplace/metrics.json","digital/lec/lec_summary.json"]'::jsonb,
  '["digital/synthesis_closure/synthesis_closure_plan.json","digital/synthesis_closure/synthesis_closure_chart.json","digital/synthesis_closure/synthesis_closure_plan.md"]'::jsonb,
  '["digital/synthesis_closure/synthesis_closure_plan.json","digital/synthesis_closure/synthesis_closure_chart.json","digital/synthesis_closure/synthesis_closure_plan.md"]'::jsonb,
  '["report","structured_data"]'::jsonb,
  '["artifact_publish","synthesis_timing_triage"]'::jsonb,
  '["python","supabase"]'::jsonb,
  '["artifact_publish","synthesis_timing_triage"]'::jsonb,
  '["python","supabase"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_capture_traceback","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','SYNTHESIS_CLOSURE_MIGRATION','studio_contract_version','v1'),
  false, true, false, 'approved', 'global', 'platform_registry', now()
from agent_template
where not exists (
  select 1 from public.agents agent
  where (agent.agent_name = agent_template.name or agent.name = agent_template.name)
    and agent.owner_id is null
);

-- Workflow rows are reasserted by source-controlled application definitions too.
-- Supabase operators can use the existing workflow canvas migration pattern if
-- they need to backfill canvas nodes for older rows.

with schema_template as (
  select * from (
    values
      (
        'Digital_Arch2Synthesis',
        '{
          "note": "Synthesis uses the generated Arch2RTL handoff as RTL input and runs the synthesis stage directly.",
          "fields": [
            {"key":"foundry","label":"Foundry","type":"text","defaultValue":"sky130"},
            {"key":"pdk","label":"PDK","type":"text","defaultValue":"sky130A"},
            {"key":"toolchain","label":"Toolchain","type":"text","defaultValue":"openlane2"},
            {"key":"target_frequency_mhz","label":"Target frequency MHz","type":"number","defaultValue":100},
            {"key":"constraints_sdc","label":"Constraints SDC","type":"text","defaultValue":""},
            {"key":"run_logic_equivalence","label":"Run logic equivalence","type":"boolean","defaultValue":true},
            {"key":"run_synthesis_readiness","label":"Run synthesis readiness","type":"boolean","defaultValue":true},
            {"key":"run_synthesis_closure_loop","label":"Run synthesis closure loop","type":"boolean","defaultValue":false},
            {"key":"max_synthesis_closure_iterations","label":"Max synthesis closure iterations","type":"number","defaultValue":1},
            {"key":"allow_synthesis_timing_repair","label":"Allow synthesis setup timing repair","type":"boolean","defaultValue":true},
            {"key":"allow_synthesis_lec_repair","label":"Allow synthesis LEC repair","type":"boolean","defaultValue":true},
            {"key":"stop_on_synthesis_closure_failure","label":"Stop downstream on synthesis closure failure","type":"boolean","defaultValue":false},
            {"key":"stop_on_synthesis_lec_failure","label":"Stop downstream on synthesis LEC failure","type":"boolean","defaultValue":false}
          ]
        }'::jsonb
      ),
      (
        'System_PD',
        '{
          "fields": [
            {"key":"foundry","label":"Foundry","type":"text","defaultValue":"sky130"},
            {"key":"pdk","label":"PDK","type":"text","defaultValue":"sky130"},
            {"key":"analog_physical_mode","label":"Analog physical mode","type":"text","defaultValue":"blackbox"},
            {"key":"generate_analog_gds","label":"Generate analog GDS","type":"boolean","defaultValue":false},
            {"key":"regenerate_lef_lib_after_gds","label":"Regenerate LEF/LIB after GDS","type":"boolean","defaultValue":true},
            {"key":"run_lef_lib_consistency","label":"Run LEF/LIB consistency","type":"boolean","defaultValue":true},
            {"key":"run_logic_equivalence","label":"Run logic equivalence","type":"boolean","defaultValue":true},
            {"key":"run_drc","label":"Run DRC","type":"boolean","defaultValue":true},
            {"key":"run_lvs","label":"Run LVS","type":"boolean","defaultValue":true},
            {"key":"run_signoff_closure_loop","label":"Run signoff closure loop","type":"boolean","defaultValue":false},
            {"key":"max_signoff_closure_iterations","label":"Max signoff closure iterations","type":"number","defaultValue":1},
            {"key":"allow_timing_repair","label":"Allow timing repair","type":"boolean","defaultValue":true},
            {"key":"allow_drc_repair","label":"Allow DRC repair","type":"boolean","defaultValue":true},
            {"key":"allow_lvs_repair","label":"Allow LVS repair","type":"boolean","defaultValue":true},
            {"key":"allow_lec_repair","label":"Allow LEC repair","type":"boolean","defaultValue":true},
            {"key":"run_synthesis_closure_loop","label":"Run synthesis closure loop","type":"boolean","defaultValue":false},
            {"key":"max_synthesis_closure_iterations","label":"Max synthesis closure iterations","type":"number","defaultValue":1},
            {"key":"allow_synthesis_timing_repair","label":"Allow synthesis setup timing repair","type":"boolean","defaultValue":true},
            {"key":"allow_synthesis_lec_repair","label":"Allow synthesis LEC repair","type":"boolean","defaultValue":true},
            {"key":"stop_on_synthesis_closure_failure","label":"Stop downstream on synthesis closure failure","type":"boolean","defaultValue":false},
            {"key":"stop_on_synthesis_lec_failure","label":"Stop downstream on synthesis LEC failure","type":"boolean","defaultValue":false}
          ]
        }'::jsonb
      )
  ) as t(app, schema)
),
updated_schema as (
  update public.product_stage_schemas p
  set schema = schema_template.schema,
      is_active = true,
      updated_at = now()
  from schema_template
  where p.app = schema_template.app
  returning p.app
)
insert into public.product_stage_schemas (app, schema, is_active, updated_at)
select app, schema, true, now()
from schema_template
where not exists (select 1 from updated_schema)
  and not exists (
    select 1 from public.product_stage_schemas p
    where p.app = schema_template.app
  );
