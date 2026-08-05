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
