-- Refresh FPGA end-to-end journeys so source of truth matches:
-- RTL/FPGA2RTL -> RTL quality -> Verification -> Synthesis -> P&R -> Timing -> Bitstream.

with templates(name, description, default_source_mode, spec_required, agents) as (
  values
    (
      'FPGA_RTL_to_Bitstream',
      'Runs an FPGA prototype flow from existing RTL through RTL quality, optional verification and verification closure, constraints, Yosys synthesis, synthesis closure, nextpnr place-and-route, timing/DRC, timing closure, bitstream handoff, and dashboard publication.',
      'paste',
      false,
      array[
        'FPGA RTL Handoff Ingest Agent',
        'FPGA RTL Quality Gate Agent',
        'Digital RTL Linting Agent',
        'Digital Synthesis Readiness Agent',
        'Digital DQA Summary Agent',
        'Digital Verification Handoff Ingest Agent',
        'Digital Functional Coverage Agent',
        'Digital Testbench Generator Agent',
        'Digital Assertions (SVA) Agent',
        'Digital Simulation Control Agent',
        'Digital Simulation Execution Agent',
        'Digital Simulation Summary Coverage Agent',
        'Digital Coverage Gap Analysis Agent',
        'Digital Failure Triage Agent',
        'Digital Failure Debug Agent',
        'Digital Closure Recommendation Agent',
        'Digital Verification Plan Update Agent',
        'Digital Coverage Plan Update Agent',
        'Digital Testcase Seed Update Agent',
        'Digital Closure Rerun Planner Agent',
        'Digital Closure Iteration Judge Agent',
        'FPGA Constraint Setup Agent',
        'FPGA Yosys Synthesis Agent',
        'FPGA Synthesis Closure Agent',
        'FPGA nextpnr Place & Route Agent',
        'FPGA Timing & DRC Agent',
        'FPGA Timing Closure Agent',
        'FPGA Bitstream Handoff Agent',
        'FPGA Dashboard Agent'
      ]::text[]
    ),
    (
      'FPGA2RTL_to_Bitstream',
      'Generates FPGA-ready RTL from design intent, runs RTL quality, optional verification and verification closure, prepares board-specific PCF/LPF constraints, then runs FPGA synthesis, place-and-route, timing, closure, bitstream handoff, and dashboard publication.',
      'generate_arch2rtl',
      true,
      array[
        'Digital Spec Agent',
        'Digital Architecture Agent',
        'Digital Microarchitecture Agent',
        'Digital Register Map Agent',
        'Digital RTL Agent',
        'Digital Power Intent (UPF-lite) Agent',
        'Digital UPF Static Check Agent',
        'Digital IP Packaging & Handoff Agent',
        'Digital Arch2RTL Dashboard Agent',
        'FPGA RTL Handoff Ingest Agent',
        'FPGA RTL Quality Gate Agent',
        'Digital RTL Linting Agent',
        'Digital Synthesis Readiness Agent',
        'Digital DQA Summary Agent',
        'Digital Verification Handoff Ingest Agent',
        'Digital Functional Coverage Agent',
        'Digital Testbench Generator Agent',
        'Digital Assertions (SVA) Agent',
        'Digital Simulation Control Agent',
        'Digital Simulation Execution Agent',
        'Digital Simulation Summary Coverage Agent',
        'Digital Coverage Gap Analysis Agent',
        'Digital Failure Triage Agent',
        'Digital Failure Debug Agent',
        'Digital Closure Recommendation Agent',
        'Digital Verification Plan Update Agent',
        'Digital Coverage Plan Update Agent',
        'Digital Testcase Seed Update Agent',
        'Digital Closure Rerun Planner Agent',
        'Digital Closure Iteration Judge Agent',
        'FPGA Constraint Setup Agent',
        'FPGA Yosys Synthesis Agent',
        'FPGA Synthesis Closure Agent',
        'FPGA nextpnr Place & Route Agent',
        'FPGA Timing & DRC Agent',
        'FPGA Timing Closure Agent',
        'FPGA Bitstream Handoff Agent',
        'FPGA Dashboard Agent'
      ]::text[]
    )
),
contracts as (
  select
    t.name,
    jsonb_build_object(
      'version', 3,
      'fields', jsonb_build_array(
        jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue',t.default_source_mode,'options',jsonb_build_array('generate_arch2rtl','from_arch2rtl','paste','repo_path')),
        jsonb_build_object('key','spec_text','label','FPGA design intent','type','textarea','required',t.spec_required),
        jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
        jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
        jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
        jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
        jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
        jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')),
        jsonb_build_object('key','family','label','FPGA family','type','text','required',false),
        jsonb_build_object('key','device','label','Device','type','text','required',false),
        jsonb_build_object('key','package','label','Package','type','text','required',false),
        jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
        jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
        jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
        jsonb_build_object('key','run_fpga_verification','label','Run FPGA verification before synthesis','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','test_intent','label','Test intent','type','textarea','required',false,'defaultValue','Run smoke verification for the FPGA RTL before synthesis. Check reset behavior, basic functional behavior, assertions, and coverage readiness.'),
        jsonb_build_object('key','verification_plan','label','Verification plan','type','textarea','required',false),
        jsonb_build_object('key','random_vs_directed','label','Stimulus','type','select','required',false,'defaultValue','both','options',jsonb_build_array('both','directed','random')),
        jsonb_build_object('key','coverage_targets','label','Coverage targets','type','textarea','required',false),
        jsonb_build_object('key','simulator_type','label','Simulator','type','select','required',false,'defaultValue','verilator','options',jsonb_build_array('verilator','icarus')),
        jsonb_build_object('key','seed_count','label','Seed count','type','number','required',false,'defaultValue',10),
        jsonb_build_object('key','enable_failure_debug','label','Failure debug','type','checkbox','required',false,'defaultValue',false),
        jsonb_build_object('key','run_fpga_verification_closure_loop','label','Run FPGA verification closure loop','type','checkbox','required',false,'defaultValue',false),
        jsonb_build_object('key','max_fpga_verification_closure_iterations','label','Verification closure tries','type','number','required',false,'defaultValue',1),
        jsonb_build_object('key','generate_bitstream','label','Generate bitstream','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','run_fpga_rtl_repair_loop','label','Run RTL pass1/pass2 repair loop','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','run_fpga_synthesis_closure_loop','label','Run synthesis closure loop','type','checkbox','required',false,'defaultValue',false),
        jsonb_build_object('key','max_fpga_synthesis_closure_iterations','label','Synthesis closure tries','type','number','required',false,'defaultValue',1),
        jsonb_build_object('key','run_fpga_timing_closure_loop','label','Run timing closure loop','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','max_fpga_timing_closure_iterations','label','Timing closure tries','type','number','required',false,'defaultValue',3),
        jsonb_build_object('key','allow_yosys_flatten','label','Allow Yosys flatten','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','allow_nextpnr_seed_sweep','label','Allow nextpnr seed sweep','type','checkbox','required',false,'defaultValue',true),
        jsonb_build_object('key','allow_frequency_relaxation','label','Suggest relaxed clock target','type','checkbox','required',false,'defaultValue',false),
        jsonb_build_object('key','context_mode','label','Context mode','type','select','required',false,'defaultValue','smart','options',jsonb_build_array('smart','full')),
        jsonb_build_object('key','hem_enabled','label','Enable HEM run memory','type','checkbox','required',false,'defaultValue',false),
        jsonb_build_object('key','hem_mode','label','HEM mode','type','select','required',false,'defaultValue','fixed','options',jsonb_build_array('fixed','adaptive')),
        jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
      )
    ) as input_contract
  from templates t
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
            'position', jsonb_build_object('x', 80 + (((ord - 1) % 6) * 240), 'y', 120 + (((ord - 1) / 6) * 180)),
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
      'input_contract', c.input_contract
    ) as definitions
  from templates t
  join contracts c on c.name = t.name
),
updated as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = 'fpga',
      is_prebuilt = true,
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
