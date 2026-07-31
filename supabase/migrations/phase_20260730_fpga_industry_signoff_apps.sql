-- FPGA production signoff capabilities. Supabase remains the workflow source of truth.

with rows(agent_name, description, entrypoint, inputs, outputs, tools, default_enabled, blocking) as (
  values
    ('FPGA Constraint and CDC/RDC Signoff Agent',
     'Signs off board I/O and clock constraints and reports CDC/RDC risks before implementation.',
     'agents.fpga.fpga_constraint_cdc_signoff_agent:run_agent',
     '["fpga.rtl_files","fpga.constraints","run_fpga_constraint_signoff"]'::jsonb,
     '["fpga/signoff/fpga_constraint_cdc_signoff_summary.json"]'::jsonb,
     '[]'::jsonb,true,true),
    ('FPGA Board Bring-up and Hardware Validation Agent',
     'Prepares or executes openFPGALoader programming and records the real-board smoke-test result.',
     'agents.fpga.fpga_board_bringup_validation_agent:run_agent',
     '["fpga.bitstream","run_fpga_hardware_validation","program_connected_fpga"]'::jsonb,
     '["fpga/hardware/fpga_hardware_validation_summary.json"]'::jsonb,
     '["openFPGALoader"]'::jsonb,false,false),
    ('FPGA Power and Device Qualification Agent',
     'Qualifies device fit, routed headroom, support tier, target frequency, and an early-stage power estimate.',
     'agents.fpga.fpga_power_device_qualification_agent:run_agent',
     '["fpga.synthesis","fpga.place_route","fpga.target"]'::jsonb,
     '["fpga/qualification/fpga_power_device_qualification_summary.json"]'::jsonb,
     '[]'::jsonb,true,false)
), updated as (
  update public.agents a
  set agent_name=r.agent_name,name=r.agent_name,loop_type='fpga',domain='fpga',
      description=r.description,script_path=r.entrypoint,entrypoint=r.entrypoint,
      execution_mode='native',inputs=r.inputs,outputs=r.outputs,artifact_paths=r.outputs,
      artifact_types='["structured_data","report"]'::jsonb,
      required_skills='["fpga_signoff","artifact_publish"]'::jsonb,required_tools=r.tools,
      skills='["fpga_signoff","artifact_publish"]'::jsonb,tools=r.tools,
      hooks='["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
      agent_spec=jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
      metadata=jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','default_enabled',r.default_enabled,'blocks_downstream_on_failure',r.blocking),
      owner_id=null,is_custom=false,is_prebuilt=true,is_marketplace=false,status='approved',
      visibility='global',source='platform_registry',updated_at=now()
  from rows r where coalesce(a.agent_name,a.name)=r.agent_name
)
insert into public.agents (
  agent_name,name,loop_type,domain,description,script_path,entrypoint,execution_mode,
  inputs,outputs,artifact_paths,artifact_types,required_skills,required_tools,agent_spec,
  skills,tools,hooks,metadata,owner_id,is_custom,is_prebuilt,is_marketplace,status,visibility,source,created_at,updated_at
)
select r.agent_name,r.agent_name,'fpga','fpga',r.description,r.entrypoint,r.entrypoint,'native',
  r.inputs,r.outputs,r.outputs,'["structured_data","report"]'::jsonb,
  '["fpga_signoff","artifact_publish"]'::jsonb,r.tools,
  jsonb_build_object('name',r.agent_name,'loop_type','fpga','domain','fpga','entrypoint',r.entrypoint,'execution_mode','native'),
  '["fpga_signoff","artifact_publish"]'::jsonb,r.tools,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","on_failure_preserve_logs","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','FPGA_AGENT_FUNCTIONS','default_enabled',r.default_enabled,'blocks_downstream_on_failure',r.blocking),
  null,false,true,false,'approved','global','platform_registry',now(),now()
from rows r
where not exists (select 1 from public.agents a where coalesce(a.agent_name,a.name)=r.agent_name);

-- Insert each agent between its production anchor and the former successor.
do $$
declare
  w record;
  item record;
  node_list jsonb;
  edge_list jsonb;
  anchor_id text;
  next_id text;
  new_id text;
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
    for item in
      select * from (values
        ('Digital RTL Linting Agent','Digital CDC Analysis Agent','fpga-cdc-analysis'),
        ('Digital CDC Analysis Agent','Digital Reset Integrity Agent','fpga-reset-integrity'),
        ('FPGA Constraint Setup Agent','FPGA Constraint and CDC/RDC Signoff Agent','fpga-constraint-cdc-signoff'),
        ('FPGA Timing Closure Agent','FPGA Power and Device Qualification Agent','fpga-power-device-qualification'),
        ('FPGA Bitstream Handoff Agent','FPGA Board Bring-up and Hardware Validation Agent','fpga-board-bringup-validation')
      ) as x(anchor_label,agent_label,node_id)
    loop
      if item.agent_label='FPGA Power and Device Qualification Agent' and w.name='FPGA_Synthesis' then continue; end if;
      if item.agent_label='FPGA Board Bring-up and Hardware Validation Agent' and w.name in ('FPGA_Synthesis','FPGA_Implementation') then continue; end if;
      if exists (
        select 1 from jsonb_array_elements(node_list) n
        where coalesce(n#>>'{data,backendLabel}',n#>>'{data,uiLabel}')=item.agent_label
      ) then continue; end if;
      select n->>'id' into anchor_id from jsonb_array_elements(node_list) n
        where coalesce(n#>>'{data,backendLabel}',n#>>'{data,uiLabel}')=item.anchor_label limit 1;
      if anchor_id is null then continue; end if;
      select e->>'target' into next_id from jsonb_array_elements(edge_list) e where e->>'source'=anchor_id limit 1;
      new_id := item.node_id;
      node_list := node_list || jsonb_build_array(jsonb_build_object(
        'id',new_id,'type','agent','position',jsonb_build_object('x',1750,'y',140),
        'data',jsonb_build_object('uiLabel',item.agent_label,'backendLabel',item.agent_label)
      ));
      edge_list := (select coalesce(jsonb_agg(e),'[]'::jsonb) from jsonb_array_elements(edge_list) e
                    where not (e->>'source'=anchor_id and e->>'target'=next_id));
      edge_list := edge_list || jsonb_build_array(jsonb_build_object('id',new_id||'-in','source',anchor_id,'target',new_id));
      if next_id is not null then
        edge_list := edge_list || jsonb_build_array(jsonb_build_object('id',new_id||'-out','source',new_id,'target',next_id));
      end if;
    end loop;
    fields := coalesce(w.definitions#>'{input_contract,fields}','[]'::jsonb);
    if not exists (select 1 from jsonb_array_elements(fields) f where f->>'key'='run_fpga_constraint_signoff') then
      fields := fields || jsonb_build_array(jsonb_build_object(
        'key','run_fpga_constraint_signoff','label','Run constraint + CDC/RDC signoff',
        'type','checkbox','required',false,'defaultValue',true
      ));
    end if;
    if w.name in ('FPGA_RTL_to_Bitstream','FPGA2RTL_to_Bitstream')
       and not exists (select 1 from jsonb_array_elements(fields) f where f->>'key'='run_fpga_hardware_validation') then
      fields := fields || jsonb_build_array(jsonb_build_object(
        'key','run_fpga_hardware_validation','label','Prepare board bring-up and hardware validation',
        'type','checkbox','required',false,'defaultValue',false
      ));
    end if;
    update public.workflows
    set nodes=node_list,edges=edge_list,
        definitions=jsonb_set(jsonb_set(jsonb_set(coalesce(definitions,'{}'::jsonb),'{nodes}',node_list,true),'{edges}',edge_list,true),'{input_contract,fields}',fields,true),
        updated_at=now()
    where id=w.id;
  end loop;
end $$;
