-- Govern the model-to-digital-IP identity in Supabase so every HEM child loop
-- receives the application-specific project and RTL top-module names.
update public.physical_ai_models
set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object(
      'digital_ip_top_module', 'adaptive_aero_control_top',
      'digital_ip_project_name', 'adaptive_aero_control'
    ),
    updated_at = now()
where model_id = 'nvidia.domino.automotive_aero';

update public.physical_ai_models
set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object(
      'digital_ip_top_module', 'motor_control_top',
      'digital_ip_project_name', 'pmsm_motor_control'
    ),
    updated_at = now()
where model_id = 'chiploop.pmsm.dq.v1';
