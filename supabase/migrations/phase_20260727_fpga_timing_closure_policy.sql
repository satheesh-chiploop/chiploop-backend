-- Compact FPGA timing-closure policy. Supabase remains the workflow source of truth.
with agent_row as (
  select
    'FPGA Timing RTL Repair Agent'::text as agent_name,
    'fpga'::text as loop_type,
    'fpga'::text as domain,
    'Proposes timing-focused RTL changes, preserves module interfaces and originals, and records before/after acceptance evidence.'::text as description,
    'agents.fpga.fpga_timing_rtl_repair_agent:run_agent'::text as entrypoint,
    '["fpga.rtl_files","fpga.timing_drc","target_frequency_mhz"]'::jsonb as inputs,
    '["fpga/closure/rtl_repair/fpga_timing_rtl_repair.json"]'::jsonb as outputs,
    '["model_gateway"]'::jsonb as tools
), updated as (
  update public.agents a
  set agent_name = r.agent_name, name = r.agent_name, loop_type = r.loop_type, domain = r.domain,
      description = r.description, script_path = r.entrypoint, entrypoint = r.entrypoint,
      execution_mode = 'native', inputs = r.inputs, outputs = r.outputs, artifact_paths = r.outputs,
      artifact_types = '["structured_data","report","rtl"]'::jsonb,
      required_skills = '["fpga_timing_closure","rtl_repair","artifact_publish"]'::jsonb,
      required_tools = r.tools, skills = '["fpga_timing_closure","rtl_repair","artifact_publish"]'::jsonb,
      tools = r.tools, hooks = '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
      agent_spec = jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
      metadata = jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','runtime_optional',true,'requires_explicit_enable',true),
      owner_id = null, is_custom = false, is_prebuilt = true, is_marketplace = false,
      status = 'approved', visibility = 'global', source = 'platform_registry', updated_at = now()
  from agent_row r where coalesce(a.agent_name,a.name) = r.agent_name returning a.agent_name
)
insert into public.agents (
  agent_name,name,loop_type,domain,description,script_path,entrypoint,execution_mode,
  inputs,outputs,artifact_paths,artifact_types,required_skills,required_tools,agent_spec,
  skills,tools,hooks,metadata,owner_id,is_custom,is_prebuilt,is_marketplace,status,visibility,source,created_at,updated_at
)
select r.agent_name,r.agent_name,r.loop_type,r.domain,r.description,r.entrypoint,r.entrypoint,'native',
  r.inputs,r.outputs,r.outputs,'["structured_data","report","rtl"]'::jsonb,
  '["fpga_timing_closure","rtl_repair","artifact_publish"]'::jsonb,r.tools,
  jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
  '["fpga_timing_closure","rtl_repair","artifact_publish"]'::jsonb,r.tools,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','runtime_optional',true,'requires_explicit_enable',true),
  null,false,true,false,'approved','global','platform_registry',now(),now()
from agent_row r where not exists (select 1 from public.agents a where coalesce(a.agent_name,a.name)=r.agent_name);

-- Add only the two simple user-facing controls; seed/synthesis knobs remain internal policy.
update public.workflows w
set definitions = jsonb_set(
      w.definitions,
      '{input_contract,fields}',
      coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb) ||
      case when not exists (
        select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb)) f where f->>'key'='fpga_closure_mode'
      ) then jsonb_build_array(jsonb_build_object('key','fpga_closure_mode','label','Closure mode','type','select','required',false,'defaultValue','balanced','options',jsonb_build_array('balanced','advanced'))) else '[]'::jsonb end ||
      case when not exists (
        select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb)) f where f->>'key'='allow_automatic_rtl_timing_repair'
      ) then jsonb_build_array(jsonb_build_object('key','allow_automatic_rtl_timing_repair','label','Automatic RTL timing repair','type','checkbox','required',false,'defaultValue',false)) else '[]'::jsonb end,
      true
    ),
    updated_at = now()
where w.user_id is null and w.is_prebuilt is true and w.loop_type = 'fpga'
  and exists (
    select 1 from jsonb_array_elements(coalesce(w.definitions->'nodes','[]'::jsonb)) node
    where coalesce(node#>>'{data,backendLabel}', node->>'label') = 'FPGA Timing Closure Agent'
  );

-- Synthesis-only workflows use the same mode selector without exposing timing repair.
update public.workflows w
set definitions = jsonb_set(
      w.definitions,
      '{input_contract,fields}',
      coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb) ||
      case when not exists (
        select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb)) f where f->>'key'='fpga_closure_mode'
      ) then jsonb_build_array(jsonb_build_object('key','fpga_closure_mode','label','Closure mode','type','select','required',false,'defaultValue','balanced','options',jsonb_build_array('balanced','advanced'))) else '[]'::jsonb end,
      true
    ),
    updated_at = now()
where w.user_id is null and w.is_prebuilt is true and w.loop_type = 'fpga'
  and exists (
    select 1 from jsonb_array_elements(coalesce(w.definitions->'nodes','[]'::jsonb)) node
    where coalesce(node#>>'{data,backendLabel}', node->>'label') = 'FPGA Synthesis Closure Agent'
  );


-- Standalone Implementation composes the same verification + synthesis + implementation
-- stage capabilities used by the reference RTL-to-Bitstream journey.
with implementation_agents(agent_name, ord) as (
  values
    ('FPGA RTL Handoff Ingest Agent',1),
    ('FPGA RTL Quality Gate Agent',2),
    ('Digital RTL Linting Agent',3),
    ('Digital Synthesis Readiness Agent',4),
    ('Digital DQA Summary Agent',5),
    ('Digital Verification Handoff Ingest Agent',6),
    ('Digital Functional Coverage Agent',7),
    ('Digital Testbench Generator Agent',8),
    ('Digital Assertions (SVA) Agent',9),
    ('Digital Formal Verification Agent',10),
    ('Digital Simulation Control Agent',11),
    ('Digital Simulation Execution Agent',12),
    ('Digital Simulation Summary Coverage Agent',13),
    ('Digital Coverage Gap Analysis Agent',14),
    ('Digital Failure Triage Agent',15),
    ('Digital Failure Debug Agent',16),
    ('Digital Closure Recommendation Agent',17),
    ('Digital Verification Plan Update Agent',18),
    ('Digital Coverage Plan Update Agent',19),
    ('Digital Testcase Seed Update Agent',20),
    ('Digital Closure Rerun Planner Agent',21),
    ('Digital Closure Iteration Judge Agent',22),
    ('FPGA Constraint Setup Agent',23),
    ('FPGA Yosys Synthesis Agent',24),
    ('FPGA Synthesis Closure Agent',25),
    ('FPGA nextpnr Place & Route Agent',26),
    ('FPGA Timing & DRC Agent',27),
    ('FPGA Timing Closure Agent',28),
    ('FPGA Dashboard Agent',29)
), graph as (
  select
    jsonb_agg(jsonb_build_object(
      'id','n' || ord,
      'type','agent',
      'position',jsonb_build_object('x',80 + (((ord-1)%5)*260),'y',120 + (((ord-1)/5)*180)),
      'data',jsonb_build_object('uiLabel',agent_name,'backendLabel',agent_name)
    ) order by ord) as nodes,
    (select jsonb_agg(jsonb_build_object('id','e' || edge_ord,'source','n' || edge_ord,'target','n' || (edge_ord+1)) order by edge_ord)
       from generate_series(1,28) edge_ord) as edges
  from implementation_agents
)
update public.workflows w
set definitions = jsonb_set(jsonb_set(w.definitions,'{nodes}',g.nodes,true),'{edges}',g.edges,true),
    nodes = g.nodes,
    edges = g.edges,
    updated_at = now()
from graph g
where w.name = 'FPGA_Implementation' and w.user_id is null and w.is_prebuilt is true;

-- Ensure standalone Implementation defaults to verification before any automatic RTL repair.
update public.workflows w
set definitions = jsonb_set(
      w.definitions,
      '{input_contract,fields}',
      coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb) ||
      case when not exists (
        select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb)) f where f->>'key'='run_fpga_verification'
      ) then jsonb_build_array(jsonb_build_object('key','run_fpga_verification','label','Run verification before implementation','type','checkbox','required',false,'defaultValue',true)) else '[]'::jsonb end,
      true
    ),
    updated_at = now()
where w.name = 'FPGA_Implementation' and w.user_id is null and w.is_prebuilt is true;
