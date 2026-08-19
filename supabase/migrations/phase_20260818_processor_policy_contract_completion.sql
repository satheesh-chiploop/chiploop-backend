-- Complete the v2 processor policy contract without replacing Supabase-owned
-- core catalogs or user/application-specific configuration.

update public.physical_ai_models
set configuration = jsonb_set(
  configuration,
  '{processor_ip_policy}',
  coalesce(configuration->'processor_ip_policy', '{}'::jsonb)
    || jsonb_build_object(
      'schema', 'chiploop.application_intelligence.processor_ip_policy.v2',
      'automatic_fpga_deployment', coalesce(configuration->'processor_ip_policy'->'automatic_fpga_deployment', '"fpga_external_host"'::jsonb),
      'automatic_asic_deployment', coalesce(configuration->'processor_ip_policy'->'automatic_asic_deployment', '"asic_digital_ip"'::jsonb),
      'fpga_hard_cpu', coalesce(configuration->'processor_ip_policy'->'fpga_hard_cpu', jsonb_build_object('availability','board_contract_required')),
      'fpga_soft_cpu', coalesce(configuration->'processor_ip_policy'->'fpga_soft_cpu', '{}'::jsonb)
        || jsonb_build_object(
          'integration_gate', coalesce(
            configuration->'processor_ip_policy'->'fpga_soft_cpu'->'integration_gate',
            jsonb_build_object(
              'cpu_rtl_required', true,
              'complete_system_synthesis_required', true,
              'default_status', 'pending_cpu_rtl'
            )
          )
        ),
      'asic_soc_cpu', coalesce(configuration->'processor_ip_policy'->'asic_soc_cpu', '{}'::jsonb)
        || jsonb_build_object(
          'integration_gate', coalesce(configuration->'processor_ip_policy'->'asic_soc_cpu'->'integration_gate', '{}'::jsonb)
            || jsonb_build_object('complete_system_synthesis_required', true)
        )
    ),
  true
), updated_at = now()
where configuration ? 'processor_ip_policy';

update public.workflows
set definitions = jsonb_set(
  definitions,
  '{processor_ip_policy}',
  coalesce(definitions->'processor_ip_policy', '{}'::jsonb)
    || jsonb_build_object(
      'schema', 'chiploop.application_intelligence.processor_ip_policy.v2',
      'automatic_fpga_deployment', coalesce(definitions->'processor_ip_policy'->'automatic_fpga_deployment', '"fpga_external_host"'::jsonb),
      'automatic_asic_deployment', coalesce(definitions->'processor_ip_policy'->'automatic_asic_deployment', '"asic_digital_ip"'::jsonb),
      'fpga_hard_cpu', coalesce(definitions->'processor_ip_policy'->'fpga_hard_cpu', jsonb_build_object('availability','board_contract_required')),
      'fpga_soft_cpu', coalesce(definitions->'processor_ip_policy'->'fpga_soft_cpu', '{}'::jsonb)
        || jsonb_build_object(
          'integration_gate', coalesce(
            definitions->'processor_ip_policy'->'fpga_soft_cpu'->'integration_gate',
            jsonb_build_object(
              'cpu_rtl_required', true,
              'complete_system_synthesis_required', true,
              'default_status', 'pending_cpu_rtl'
            )
          )
        ),
      'asic_soc_cpu', coalesce(definitions->'processor_ip_policy'->'asic_soc_cpu', '{}'::jsonb)
        || jsonb_build_object(
          'integration_gate', coalesce(definitions->'processor_ip_policy'->'asic_soc_cpu'->'integration_gate', '{}'::jsonb)
            || jsonb_build_object('complete_system_synthesis_required', true)
        )
    ),
  true
), updated_at = now()
where name = 'Physical_AI_Loop' and user_id is null and definitions ? 'processor_ip_policy';
