-- FPGA RTL-to-synthesis-netlist LEC. Supabase remains workflow source of truth.

with agent_row as (
  select
    'FPGA RTL-to-Netlist Equivalence Agent'::text agent_name,
    'fpga'::text loop_type,
    'fpga'::text domain,
    'Uses Yosys equivalence passes to prove the winning synthesized FPGA netlist matches the approved RTL. Blocks downstream implementation when enabled and not proven.'::text description,
    'agents.fpga.fpga_logic_equivalence_agent:run_agent'::text entrypoint,
    '["fpga.rtl_files","fpga.synthesis.verilog_netlist","run_fpga_lec"]'::jsonb inputs,
    '["fpga/lec/fpga_lec_summary.json","fpga/lec/fpga_rtl_to_netlist_lec.ys","fpga/lec/fpga_rtl_to_netlist_lec.log"]'::jsonb outputs
), updated as (
  update public.agents a
  set agent_name=r.agent_name,name=r.agent_name,loop_type=r.loop_type,domain=r.domain,
      description=r.description,script_path=r.entrypoint,entrypoint=r.entrypoint,
      execution_mode='native',inputs=r.inputs,outputs=r.outputs,artifact_paths=r.outputs,
      artifact_types='["structured_data","report","log","yosys_script"]'::jsonb,
      required_skills='["fpga_lec","formal_equivalence","artifact_publish"]'::jsonb,
      required_tools='["yosys"]'::jsonb,
      skills='["fpga_lec","formal_equivalence","artifact_publish"]'::jsonb,
      tools='["yosys"]'::jsonb,
      hooks='["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
      agent_spec=jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
      metadata=jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','default_enabled',true,'blocks_downstream_on_failure',true),
      owner_id=null,is_custom=false,is_prebuilt=true,is_marketplace=false,
      status='approved',visibility='global',source='platform_registry',updated_at=now()
  from agent_row r where coalesce(a.agent_name,a.name)=r.agent_name
)
insert into public.agents (
  agent_name,name,loop_type,domain,description,script_path,entrypoint,execution_mode,
  inputs,outputs,artifact_paths,artifact_types,required_skills,required_tools,agent_spec,
  skills,tools,hooks,metadata,owner_id,is_custom,is_prebuilt,is_marketplace,status,visibility,source,created_at,updated_at
)
select r.agent_name,r.agent_name,r.loop_type,r.domain,r.description,r.entrypoint,r.entrypoint,'native',
  r.inputs,r.outputs,r.outputs,'["structured_data","report","log","yosys_script"]'::jsonb,
  '["fpga_lec","formal_equivalence","artifact_publish"]'::jsonb,'["yosys"]'::jsonb,
  jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
  '["fpga_lec","formal_equivalence","artifact_publish"]'::jsonb,'["yosys"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','default_enabled',true,'blocks_downstream_on_failure',true),
  null,false,true,false,'approved','global','platform_registry',now(),now()
from agent_row r
where not exists (select 1 from public.agents a where coalesce(a.agent_name,a.name)=r.agent_name);

-- Insert LEC after synthesis closure (or synthesis when closure is absent) and before its former successor.
do $$
declare
  w record;
  node_list jsonb;
  edge_list jsonb;
  anchor_id text;
  next_id text;
  lec_id text := 'fpga-rtl-netlist-lec';
  lec_node jsonb;
  fields jsonb;
begin
  for w in
    select id,name,definitions,nodes,edges from public.workflows
    where user_id is null and name in (
      'FPGA_RTL_to_Bitstream','FPGA2RTL_to_Bitstream','FPGA_Synthesis','FPGA_Implementation'
    )
  loop
    node_list := coalesce(w.definitions->'nodes',w.nodes,'[]'::jsonb);
    edge_list := coalesce(w.definitions->'edges',w.edges,'[]'::jsonb);
    select n->>'id' into anchor_id from jsonb_array_elements(node_list) n
      where coalesce(n#>>'{data,backendLabel}',n#>>'{data,uiLabel}')='FPGA Synthesis Closure Agent' limit 1;
    if anchor_id is null then
      select n->>'id' into anchor_id from jsonb_array_elements(node_list) n
        where coalesce(n#>>'{data,backendLabel}',n#>>'{data,uiLabel}')='FPGA Yosys Synthesis Agent' limit 1;
    end if;
    if anchor_id is null then continue; end if;
    select e->>'target' into next_id from jsonb_array_elements(edge_list) e where e->>'source'=anchor_id limit 1;
    lec_node := jsonb_build_object(
      'id',lec_id,'type','agent','position',jsonb_build_object('x',1500,'y',140),
      'data',jsonb_build_object('uiLabel','FPGA RTL-to-Netlist Equivalence Agent','backendLabel','FPGA RTL-to-Netlist Equivalence Agent')
    );
    if not exists (select 1 from jsonb_array_elements(node_list) n where coalesce(n#>>'{data,backendLabel}',n#>>'{data,uiLabel}')='FPGA RTL-to-Netlist Equivalence Agent') then
      node_list := node_list || jsonb_build_array(lec_node);
      edge_list := (select coalesce(jsonb_agg(e),'[]'::jsonb) from jsonb_array_elements(edge_list) e
                    where not (e->>'source'=anchor_id and e->>'target'=next_id));
      edge_list := edge_list || jsonb_build_array(jsonb_build_object('id','fpga-lec-in','source',anchor_id,'target',lec_id));
      if next_id is not null then
        edge_list := edge_list || jsonb_build_array(jsonb_build_object('id','fpga-lec-out','source',lec_id,'target',next_id));
      end if;
    end if;
    fields := coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb);
    if not exists (select 1 from jsonb_array_elements(fields) f where f->>'key'='run_fpga_lec') then
      fields := fields || jsonb_build_array(jsonb_build_object(
        'key','run_fpga_lec','label','Run RTL-to-netlist equivalence (LEC)',
        'type','checkbox','required',false,'defaultValue',true
      ));
    end if;
    update public.workflows
    set nodes=node_list,edges=edge_list,
        definitions=jsonb_set(jsonb_set(jsonb_set(coalesce(definitions,'{}'::jsonb),'{nodes}',node_list,true),'{edges}',edge_list,true),'{input_contract,fields}',fields,true),
        updated_at=now()
    where id=w.id;
  end loop;
end $$;
