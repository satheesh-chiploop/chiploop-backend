-- FPGA Verify source-of-truth refresh.
-- Adds FPGA-specific verification templates while reusing existing verification agents.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

with templates(name, description, agents, input_contract) as (
  values
    (
      'FPGA_Verify',
      'Runs FPGA-focused testbench generation, assertions, simulation, coverage, optional formal/golden checks, and verification dashboard evidence.',
      array[
        'Digital Verification Handoff Ingest Agent',
        'Digital Functional Coverage Agent',
        'Digital Testbench Generator Agent',
        'Digital Assertions (SVA) Agent',
        'Digital Simulation Control Agent',
        'Digital Simulation Execution Agent',
        'Digital Simulation Summary Coverage Agent'
      ]::text[],
      jsonb_build_object(
        'version', 1,
        'fields', jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path')),
          jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','source_arch2rtl_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
          jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
          jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
          jsonb_build_object('key','test_intent','label','Test intent','type','textarea','required',true),
          jsonb_build_object('key','verification_plan','label','Verification plan','type','textarea','required',false),
          jsonb_build_object('key','random_vs_directed','label','Stimulus','type','select','required',false,'defaultValue','both','options',jsonb_build_array('both','directed','random')),
          jsonb_build_object('key','coverage_targets','label','Coverage targets','type','textarea','required',false),
          jsonb_build_object('key','simulator_type','label','Simulator','type','select','required',false,'defaultValue','verilator','options',jsonb_build_array('verilator','icarus')),
          jsonb_build_object('key','seed_count','label','Seed count','type','number','required',false,'defaultValue',10),
          jsonb_build_object('key','toggles','label','Verification toggles','type','json','required',false),
          jsonb_build_object('key','toolchain','label','Toolchain','type','json','required',false),
          jsonb_build_object('key','enable_failure_debug','label','Failure debug','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','run_closure_analysis','label','Run closure analysis','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
        )
      )
    ),
    (
      'FPGA_Verify_Closure_Loop',
      'Runs bounded FPGA verification closure iterations using coverage gaps, failure triage, plan updates, testcase seed updates, rerun planning, and simulation reruns.',
      array[
        'Digital Verify Closure Ingest Agent',
        'Digital Coverage Gap Analysis Agent',
        'Digital Failure Triage Agent',
        'Digital Failure Debug Agent',
        'Digital Closure Recommendation Agent',
        'Digital Verification Plan Update Agent',
        'Digital Coverage Plan Update Agent',
        'Digital Testcase Seed Update Agent',
        'Digital Closure Rerun Planner Agent',
        'Digital Verification Handoff Ingest Agent',
        'Digital Testbench Generator Agent',
        'Digital Assertions (SVA) Agent',
        'Digital Functional Coverage Agent',
        'Digital Simulation Control Agent',
        'Digital Simulation Execution Agent',
        'Digital Simulation Summary Coverage Agent',
        'Digital Closure Iteration Judge Agent'
      ]::text[],
      jsonb_build_object(
        'version', 1,
        'fields', jsonb_build_array(
          jsonb_build_object('key','source_verify_workflow_id','label','Source FPGA Verify workflow ID','type','text','required',true),
          jsonb_build_object('key','coverage_targets','label','Coverage targets','type','textarea','required',false),
          jsonb_build_object('key','seed_count','label','Seed count','type','number','required',false,'defaultValue',10),
          jsonb_build_object('key','seed_budget','label','Seed budget','type','number','required',false,'defaultValue',10),
          jsonb_build_object('key','max_iterations','label','Closure iterations','type','number','required',false,'defaultValue',1),
          jsonb_build_object('key','rerun_mode','label','Rerun mode','type','select','required',false,'defaultValue','coverage_targeted','options',jsonb_build_array('coverage_targeted','failed_only','full_regression')),
          jsonb_build_object('key','random_vs_directed','label','Stimulus','type','select','required',false,'defaultValue','both','options',jsonb_build_array('both','directed','random')),
          jsonb_build_object('key','enable_failure_debug','label','Failure debug','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','toolchain','label','Toolchain','type','json','required',false)
        )
      )
    )
),
definitions as (
  select
    t.name,
    jsonb_build_object(
      'nodes',
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', 'n' || ord,
            'type', 'agent',
            'position', jsonb_build_object('x', 80 + (((ord - 1) % 5) * 260), 'y', 140 + (((ord - 1) / 5) * 190)),
            'data', jsonb_build_object('uiLabel', agent_name, 'backendLabel', agent_name)
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
      'description', t.description,
      'category', 'fpga',
      'source_of_truth', 'supabase',
      'input_contract', t.input_contract
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
      status = coalesce(w.status, 'saved'),
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

do $$
begin
  if to_regclass('public.apps') is not null then
    update public.apps a
    set loop_type = 'fpga',
        updated_at = now()
    where a.slug in ('fpga-verify')
       or a.name in ('FPGA Verify', 'FPGA_Verify');
  end if;
end $$;

create index if not exists idx_workflows_prebuilt_name
  on public.workflows(name)
  where is_prebuilt = true;
