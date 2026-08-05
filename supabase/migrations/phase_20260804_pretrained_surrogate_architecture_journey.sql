-- CPU-only architecture reference journey for a real pretrained NVIDIA surrogate.
-- Supabase remains the catalog source of truth; no inference result is asserted.
update public.physical_ai_models
set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object(
      'version', 2,
      'checkpoint', 'nvidia/domino_drivaerml',
      'reference_url', 'https://huggingface.co/nvidia/domino_drivaerml',
      'architecture_definition_supported', true,
      'reference_application', 'automotive_aerodynamics_architecture'
    ),
    updated_at = now()
where model_id = 'nvidia.domino.automotive_aero';

-- Keep requires_gpu_worker because it remains true for inference.
update public.workflows
set definitions = coalesce(definitions, '{}'::jsonb) || jsonb_build_object(
      'supports_architecture_mode', true,
      'architecture_reference_model_id', 'nvidia.domino.automotive_aero',
      'architecture_next_loop', 'digital_design',
      'architecture_execution_modes', jsonb_build_array('architecture', 'validated'),
      'implementation_paths', jsonb_build_array('architecture_only', 'digital_ip_asic', 'fpga_prototype', 'fpga_then_asic'),
      'surrogate_inference_required', false,
      'schema_version', 2
    ),
    updated_at = now()
where name = 'Physical_AI_Loop' and user_id is null;

-- Add the model-driven architecture agent to the public Studio workflow.
update public.workflows
set nodes = jsonb_build_array(
      jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',40,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Requirements Agent','backendLabel','Physical AI Requirements Agent')),
      jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',300,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Model Selection Agent','backendLabel','Physical AI Model Selection Agent')),
      jsonb_build_object('id','n3','type','agent','position',jsonb_build_object('x',560,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Physics Execution Agent','backendLabel','Physical AI Physics Execution Agent')),
      jsonb_build_object('id','n4','type','agent','position',jsonb_build_object('x',820,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Architecture Agent','backendLabel','Physical AI Architecture Agent')),
      jsonb_build_object('id','n5','type','agent','position',jsonb_build_object('x',1080,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Orchestrator Agent','backendLabel','Physical AI Orchestrator Agent'))
    ),
    edges = jsonb_build_array(
      jsonb_build_object('id','e1','source','n1','target','n2'),
      jsonb_build_object('id','e2','source','n2','target','n3'),
      jsonb_build_object('id','e3','source','n3','target','n4'),
      jsonb_build_object('id','e4','source','n4','target','n5')
    ),
    definitions = coalesce(definitions, '{}'::jsonb) || jsonb_build_object('agent_count',5,'hem_paths',jsonb_build_object(
      'fpga_prototype',jsonb_build_array('arch2rtl','verify','fpga_exploration','fpga_bitstream'),
      'digital_ip_asic',jsonb_build_array('arch2rtl','verify','arch2tapeout'),
      'fpga_then_asic',jsonb_build_array('arch2rtl','verify','fpga_exploration','fpga_bitstream','arch2tapeout')
    )),
    updated_at = now()
where name = 'Physical_AI_Loop' and user_id is null;

update public.workflows
set definitions = jsonb_set(jsonb_set(coalesce(definitions, '{}'::jsonb), '{nodes}', nodes, true), '{edges}', edges, true),
    updated_at = now()
where name = 'Physical_AI_Loop' and user_id is null;

insert into public.agents (
  agent_name,name,loop_type,domain,description,script_path,entrypoint,execution_mode,
  inputs,outputs,artifact_paths,artifact_types,required_skills,required_tools,agent_spec,
  skills,tools,hooks,metadata,owner_id,is_custom,is_prebuilt,is_marketplace,status,visibility,source,created_at,updated_at
)
select 'Physical AI Architecture Agent','Physical AI Architecture Agent','physical_ai','physical_ai',
  'Uses the selected agent model and physics-model evidence to generate a product architecture and an Arch2RTL-ready specification.',
  'agents.physical_ai.physical_ai_architecture_agent:run_agent','agents.physical_ai.physical_ai_architecture_agent:run_agent','native',
  '["physical_ai requirements","selected physics model","physics evidence"]'::jsonb,
  '["physical_ai/model_generated_architecture.json"]'::jsonb,
  '["physical_ai/model_generated_architecture.json","physical_ai/model_generated_architecture_raw.txt"]'::jsonb,
  '["structured_data","report"]'::jsonb,'["artifact_publish"]'::jsonb,'["llm","python","supabase"]'::jsonb,
  jsonb_build_object('name','Physical AI Architecture Agent','loop_type','physical_ai','domain','physical_ai','entrypoint','agents.physical_ai.physical_ai_architecture_agent:run_agent','execution_mode','native'),
  '["artifact_publish"]'::jsonb,'["llm","python","supabase"]'::jsonb,
  '["pre_run_validate_inputs","post_run_collect_artifacts","post_run_update_state","artifact_publish_to_supabase"]'::jsonb,
  jsonb_build_object('registry_source','PHYSICAL_AI_AGENT_FUNCTIONS','default_enabled',true),
  null,false,true,false,'approved','global','platform_registry',now(),now()
where not exists (select 1 from public.agents where coalesce(agent_name,name)='Physical AI Architecture Agent');
