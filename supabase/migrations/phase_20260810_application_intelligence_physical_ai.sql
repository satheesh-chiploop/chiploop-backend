-- Application Intelligence expansion of the Physical AI loop.
-- Supabase remains the source of truth for model and workflow discovery.

update public.physical_ai_models
set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object(
      'cpu_reference_supported', true,
      'cpu_reference_type', 'analytical_drag_reference',
      'surrogate_inference_claimed', false,
      'reference_application', 'intelligent_active_aerodynamics_controller'
    ),
    updated_at = now()
where model_id = 'nvidia.domino.automotive_aero';

with graph as (
  select jsonb_build_array(
    jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',40,'y',100),'data',jsonb_build_object('uiLabel','Physical AI Requirements Agent','backendLabel','Physical AI Requirements Agent')),
    jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',300,'y',100),'data',jsonb_build_object('uiLabel','Application Intelligence Agent','backendLabel','Application Intelligence Agent')),
    jsonb_build_object('id','n3','type','agent','position',jsonb_build_object('x',560,'y',100),'data',jsonb_build_object('uiLabel','Physical AI Model Selection Agent','backendLabel','Physical AI Model Selection Agent')),
    jsonb_build_object('id','n4','type','agent','position',jsonb_build_object('x',820,'y',100),'data',jsonb_build_object('uiLabel','Surrogate Discovery and Mapping Agent','backendLabel','Surrogate Discovery and Mapping Agent')),
    jsonb_build_object('id','n5','type','agent','position',jsonb_build_object('x',1080,'y',100),'data',jsonb_build_object('uiLabel','Physical AI Physics Execution Agent','backendLabel','Physical AI Physics Execution Agent')),
    jsonb_build_object('id','n6','type','agent','position',jsonb_build_object('x',1340,'y',100),'data',jsonb_build_object('uiLabel','Physical AI Architecture Agent','backendLabel','Physical AI Architecture Agent')),
    jsonb_build_object('id','n7','type','agent','position',jsonb_build_object('x',1600,'y',100),'data',jsonb_build_object('uiLabel','Hardware Software Partitioning Agent','backendLabel','Hardware Software Partitioning Agent')),
    jsonb_build_object('id','n8','type','agent','position',jsonb_build_object('x',1860,'y',100),'data',jsonb_build_object('uiLabel','Physical AI Orchestrator Agent','backendLabel','Physical AI Orchestrator Agent'))
  ) nodes
), edges as (
  select jsonb_build_array(
    jsonb_build_object('id','e1','source','n1','target','n2'), jsonb_build_object('id','e2','source','n2','target','n3'),
    jsonb_build_object('id','e3','source','n3','target','n4'), jsonb_build_object('id','e4','source','n4','target','n5'),
    jsonb_build_object('id','e5','source','n5','target','n6'), jsonb_build_object('id','e6','source','n6','target','n7'),
    jsonb_build_object('id','e7','source','n7','target','n8')
  ) edges
)
update public.workflows w
set nodes = g.nodes,
    edges = e.edges,
    definitions = coalesce(w.definitions, '{}'::jsonb) || jsonb_build_object(
      'nodes', g.nodes, 'edges', e.edges, 'agent_count', 8,
      'description', 'Understands an application, discovers and qualifies model candidates, executes a CPU reference or qualified surrogate, partitions the intelligent system, and delegates to existing implementation loops.',
      'supports_application_intelligence', true,
      'execution_modes', jsonb_build_array('architecture','cpu_reference','validated'),
      'reference_journey', 'intelligent_active_aerodynamics_controller',
      'schema_version', 3
    ),
    updated_at = now()
from graph g, edges e
where w.name = 'Physical_AI_Loop' and w.user_id is null;

with new_agents(agent_name, description, entrypoint, inputs, outputs) as (
  values
    ('Application Intelligence Agent', 'Converts application intent into constraints, capabilities, and measurable acceptance gates.', 'agents.physical_ai.physical_ai_application_intelligence_agent:run_agent', '["physical_ai/requirements_contract.json"]'::jsonb, '["physical_ai/application_contract.json"]'::jsonb),
    ('Surrogate Discovery and Mapping Agent', 'Ranks governed equation and pretrained surrogate candidates and records qualification limits without fabricating inference.', 'agents.physical_ai.physical_ai_surrogate_mapping_agent:run_agent', '["physical_ai/application_contract.json","physical_ai_models"]'::jsonb, '["physical_ai/surrogate_mapping.json"]'::jsonb),
    ('Hardware Software Partitioning Agent', 'Partitions application jobs across software, firmware, GPU services, FPGA, and ASIC targets with explicit interfaces.', 'agents.physical_ai.physical_ai_partitioning_agent:run_agent', '["physical_ai/model_generated_architecture.json","physical_ai/surrogate_mapping.json"]'::jsonb, '["physical_ai/partition_plan.json"]'::jsonb)
), updated as (
  update public.agents a set
    agent_name=n.agent_name, name=n.agent_name, loop_type='physical_ai', domain='physical_ai', description=n.description,
    script_path=n.entrypoint, entrypoint=n.entrypoint, execution_mode='native', inputs=n.inputs, outputs=n.outputs,
    artifact_paths=n.outputs, artifact_types='["structured_data","report"]'::jsonb,
    required_skills='["artifact_publish"]'::jsonb, required_tools='["python","supabase"]'::jsonb,
    skills='["artifact_publish"]'::jsonb, tools='["python","supabase"]'::jsonb,
    hooks='["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","artifact_publish_to_supabase"]'::jsonb,
    agent_spec=jsonb_build_object('name',n.agent_name,'loop_type','physical_ai','domain','physical_ai','entrypoint',n.entrypoint,'execution_mode','native'),
    metadata=jsonb_build_object('registry_source','PHYSICAL_AI_AGENT_FUNCTIONS','default_enabled',true,'application_intelligence',true),
    owner_id=null,is_custom=false,is_prebuilt=true,is_marketplace=false,status='approved',visibility='global',source='platform_registry',updated_at=now()
  from new_agents n where coalesce(a.agent_name,a.name)=n.agent_name
)
insert into public.agents (
  agent_name,name,loop_type,domain,description,script_path,entrypoint,execution_mode,inputs,outputs,artifact_paths,
  artifact_types,required_skills,required_tools,agent_spec,skills,tools,hooks,metadata,owner_id,is_custom,is_prebuilt,
  is_marketplace,status,visibility,source,created_at,updated_at
)
select n.agent_name,n.agent_name,'physical_ai','physical_ai',n.description,n.entrypoint,n.entrypoint,'native',n.inputs,n.outputs,n.outputs,
  '["structured_data","report"]'::jsonb,'["artifact_publish"]'::jsonb,'["python","supabase"]'::jsonb,
  jsonb_build_object('name',n.agent_name,'loop_type','physical_ai','domain','physical_ai','entrypoint',n.entrypoint,'execution_mode','native'),
  '["artifact_publish"]'::jsonb,'["python","supabase"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','PHYSICAL_AI_AGENT_FUNCTIONS','default_enabled',true,'application_intelligence',true),
  null,false,true,false,'approved','global','platform_registry',now(),now()
from new_agents n
where not exists (select 1 from public.agents a where coalesce(a.agent_name,a.name)=n.agent_name);
