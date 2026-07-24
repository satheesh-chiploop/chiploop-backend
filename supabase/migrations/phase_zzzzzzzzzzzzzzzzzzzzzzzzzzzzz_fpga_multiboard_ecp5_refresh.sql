-- Refresh FPGA source-of-truth contract for multi-board open-source prototyping.
-- Adds iCE40 HX8K, ECP5, and an optional Arch2RTL-from-design-intent source mode.

with fpga_contract as (
  select jsonb_build_object(
    'version', 2,
    'fields',
    jsonb_build_array(
      jsonb_build_object(
        'key','rtl_source_mode',
        'label','RTL source',
        'type','select',
        'required',true,
        'defaultValue','paste',
        'options',jsonb_build_array('generate_arch2rtl','from_arch2rtl','paste','repo_path')
      ),
      jsonb_build_object('key','spec_text','label','Design intent','type','textarea','required',false),
      jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
      jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
      jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
      jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
      jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
      jsonb_build_object(
        'key','board',
        'label','Board',
        'type','select',
        'required',true,
        'defaultValue','icebreaker',
        'options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')
      ),
      jsonb_build_object('key','family','label','FPGA family','type','text','required',false),
      jsonb_build_object('key','device','label','Device','type','text','required',false),
      jsonb_build_object('key','package','label','Package','type','text','required',false),
      jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
      jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
      jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
      jsonb_build_object('key','generate_bitstream','label','Generate bitstream','type','checkbox','required',false,'defaultValue',true),
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
)
update public.workflows w
set definitions = jsonb_set(
      coalesce(w.definitions, '{}'::jsonb),
      '{input_contract}',
      c.input_contract,
      true
    ),
    loop_type = 'fpga',
    is_prebuilt = true,
    updated_at = now()
from fpga_contract c
where w.name = 'FPGA_RTL_to_Bitstream'
  and w.user_id is null;

with templates(name, description, agents) as (
  values
    (
      'FPGA2RTL_to_Bitstream',
      'Generates FPGA-ready RTL from design intent, prepares board-specific PCF/LPF constraints, then runs FPGA synthesis, place-and-route, timing, closure, bitstream handoff, and dashboard publication.',
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
contract as (
  select jsonb_build_object(
    'version', 1,
    'fields',
    jsonb_build_array(
      jsonb_build_object('key','project_name','label','Project name','type','text','required',false),
      jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
      jsonb_build_object('key','design_language','label','Design language','type','text','required',false,'defaultValue','systemverilog'),
      jsonb_build_object('key','spec_text','label','FPGA design intent','type','textarea','required',true),
      jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')),
      jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12),
      jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
      jsonb_build_object('key','generate_bitstream','label','Generate bitstream','type','checkbox','required',false,'defaultValue',true),
      jsonb_build_object('key','run_fpga_synthesis_closure_loop','label','Run synthesis closure loop','type','checkbox','required',false,'defaultValue',false),
      jsonb_build_object('key','run_fpga_timing_closure_loop','label','Run timing closure loop','type','checkbox','required',false,'defaultValue',true),
      jsonb_build_object('key','context_mode','label','Context mode','type','select','required',false,'defaultValue','smart','options',jsonb_build_array('smart','full')),
      jsonb_build_object('key','hem_enabled','label','Enable HEM run memory','type','checkbox','required',false,'defaultValue',false),
      jsonb_build_object('key','hem_mode','label','HEM mode','type','select','required',false,'defaultValue','fixed','options',jsonb_build_array('fixed','adaptive')),
      jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
    )
  ) as input_contract
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
  cross join contract c
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
  if to_regclass('public.apps') is not null
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'input_contract'
     )
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'slug'
     )
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'name'
     )
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'updated_at'
     ) then
    with fpga_contract as (
      select jsonb_build_object(
        'version', 2,
        'fields',
        jsonb_build_array(
          jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','paste','options',jsonb_build_array('generate_arch2rtl','from_arch2rtl','paste','repo_path')),
          jsonb_build_object('key','spec_text','label','Design intent','type','textarea','required',false),
          jsonb_build_object('key','board','label','Board','type','select','required',true,'defaultValue','icebreaker','options',jsonb_build_array('icebreaker','ice40_hx8k_breakout','ulx3s_ecp5_45f','upduino_v3','icestick','custom_ice40')),
          jsonb_build_object('key','pcf_text','label','Pin constraints PCF / LPF','type','textarea','required',false),
          jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',false,'defaultValue',12)
        )
      ) as input_contract
    )
    update public.apps a
    set input_contract = c.input_contract,
        updated_at = now()
    from fpga_contract c
    where a.slug in ('fpga-bitstream', 'fpga-rtl-to-bitstream', 'fpga2rtl')
       or a.name in ('FPGA RTL to Bitstream', 'FPGA RTL-to-Bitstream', 'FPGA2RTL');
  end if;
end $$;
