-- Adds standalone FPGA Formal, FPGA Synthesis, and FPGA Implementation workflows.
-- Supabase remains the source of truth for prebuilt workflow templates.

with templates(name, description, agents, input_contract) as (
  values
    (
      'FPGA_Verify',
      'Runs FPGA-focused testbench generation, assertions, optional formal checks, simulation, coverage, optional golden checks, and verification dashboard evidence.',
      array[
        'Digital Verification Handoff Ingest Agent',
        'Digital Functional Coverage Agent',
        'Digital Testbench Generator Agent',
        'Digital Assertions (SVA) Agent',
        'Digital Formal Verification Agent',
        'Digital Simulation Control Agent',
        'Digital Simulation Execution Agent',
        'Digital Simulation Summary Coverage Agent'
      ]::text[],
      jsonb_build_object(
        'version', 2,
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
          jsonb_build_object('key','formal_tool','label','Formal tool','type','select','required',false,'defaultValue','none','options',jsonb_build_array('none','symbiyosys')),
          jsonb_build_object('key','formal_solver','label','Formal solver','type','select','required',false,'defaultValue','z3','options',jsonb_build_array('z3','boolector')),
          jsonb_build_object('key','toggles','label','Verification toggles','type','json','required',false),
          jsonb_build_object('key','toolchain','label','Toolchain','type','json','required',false),
          jsonb_build_object('key','enable_failure_debug','label','Failure debug','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','run_closure_analysis','label','Run closure analysis','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
        )
      )
    ),
    (
      'FPGA_Formal',
      'Runs FPGA-focused formal verification from generated, pasted, or repository RTL using SymbiYosys and a selected solver.',
      array[
        'Digital Verification Handoff Ingest Agent',
        'Digital Formal Verification Agent'
      ]::text[],
      jsonb_build_object(
        'version', 1,
        'fields', jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path','generate_arch2rtl')),
          jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
          jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
          jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
          jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
          jsonb_build_object('key','toolchain','label','Formal toolchain','type','json','required',false),
          jsonb_build_object('key','formal_tool','label','Formal tool','type','select','required',false,'defaultValue','symbiyosys','options',jsonb_build_array('symbiyosys')),
          jsonb_build_object('key','formal_solver','label','Formal solver','type','select','required',false,'defaultValue','z3','options',jsonb_build_array('z3','boolector')),
          jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
        )
      )
    ),
    (
      'FPGA_Synthesis',
      'Runs FPGA RTL handoff, RTL quality, board constraints, Yosys synthesis, optional synthesis closure, and dashboard publication.',
      array[
        'FPGA RTL Handoff Ingest Agent',
        'FPGA RTL Quality Gate Agent',
        'Digital RTL Linting Agent',
        'Digital Synthesis Readiness Agent',
        'Digital DQA Summary Agent',
        'FPGA Constraint Setup Agent',
        'FPGA Yosys Synthesis Agent',
        'FPGA Synthesis Closure Agent',
        'FPGA Dashboard Agent'
      ]::text[],
      jsonb_build_object(
        'version', 1,
        'fields', jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path')),
          jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
          jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
          jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
          jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')),
          jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
          jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
          jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
          jsonb_build_object('key','run_fpga_rtl_repair_loop','label','Run RTL pass1/pass2 repair loop','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','run_fpga_synthesis_closure_loop','label','Run synthesis closure loop','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','max_fpga_synthesis_closure_iterations','label','Synthesis closure tries','type','number','required',false,'defaultValue',1),
          jsonb_build_object('key','allow_yosys_flatten','label','Allow Yosys flatten','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','context_mode','label','Context mode','type','select','required',false,'defaultValue','smart','options',jsonb_build_array('smart','full')),
          jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
        )
      )
    ),
    (
      'FPGA_Implementation',
      'Runs FPGA RTL handoff, quality, constraints, Yosys synthesis, nextpnr place-and-route, timing/DRC, optional timing closure, and dashboard publication.',
      array[
        'FPGA RTL Handoff Ingest Agent',
        'FPGA RTL Quality Gate Agent',
        'Digital RTL Linting Agent',
        'Digital Synthesis Readiness Agent',
        'Digital DQA Summary Agent',
        'FPGA Constraint Setup Agent',
        'FPGA Yosys Synthesis Agent',
        'FPGA Synthesis Closure Agent',
        'FPGA nextpnr Place & Route Agent',
        'FPGA Timing & DRC Agent',
        'FPGA Timing Closure Agent',
        'FPGA Dashboard Agent'
      ]::text[],
      jsonb_build_object(
        'version', 1,
        'fields', jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path')),
          jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
          jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
          jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
          jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
          jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')),
          jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
          jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
          jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
          jsonb_build_object('key','run_fpga_rtl_repair_loop','label','Run RTL pass1/pass2 repair loop','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','run_fpga_synthesis_closure_loop','label','Run synthesis closure loop','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','max_fpga_synthesis_closure_iterations','label','Synthesis closure tries','type','number','required',false,'defaultValue',1),
          jsonb_build_object('key','run_fpga_timing_closure_loop','label','Run timing closure loop','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','max_fpga_timing_closure_iterations','label','Timing closure tries','type','number','required',false,'defaultValue',3),
          jsonb_build_object('key','allow_yosys_flatten','label','Allow Yosys flatten','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','allow_nextpnr_seed_sweep','label','Allow nextpnr seed sweep','type','checkbox','required',false,'defaultValue',true),
          jsonb_build_object('key','allow_frequency_relaxation','label','Suggest relaxed clock target','type','checkbox','required',false,'defaultValue',false),
          jsonb_build_object('key','context_mode','label','Context mode','type','select','required',false,'defaultValue','smart','options',jsonb_build_array('smart','full')),
          jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
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
            'position', jsonb_build_object('x', 80 + (((ord - 1) % 5) * 260), 'y', 120 + (((ord - 1) / 5) * 180)),
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
updated as (
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
    where a.slug in ('fpga-verify', 'fpga-formal', 'fpga-synthesis', 'fpga-implementation')
       or a.name in ('FPGA Verify', 'FPGA Formal', 'FPGA Synthesis', 'FPGA Implementation');
  end if;
end $$;
