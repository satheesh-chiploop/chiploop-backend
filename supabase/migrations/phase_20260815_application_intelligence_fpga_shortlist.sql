-- Keep the Application Intelligence reference journey fast and repeatable.
-- Supabase remains the source of truth for its governed FPGA shortlist.

update public.physical_ai_models
set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object(
      'reference_fpga_candidate_boards', jsonb_build_array(
        'ulx3s_ecp5_45f',
        'orangecrab_ecp5_85f'
      ),
      'reference_fpga_candidate_policy', 'application_intelligence_active_aero_v1'
    ),
    updated_at = now()
where model_id = 'nvidia.domino.automotive_aero';

update public.workflows
set definitions = coalesce(definitions, '{}'::jsonb) || jsonb_build_object(
      'reference_fpga_candidate_boards', jsonb_build_array(
        'ulx3s_ecp5_45f',
        'orangecrab_ecp5_85f'
      ),
      'reference_fpga_candidate_policy', 'application_intelligence_active_aero_v1'
    ),
    updated_at = now()
where name = 'Physical_AI_Loop'
  and user_id is null;
