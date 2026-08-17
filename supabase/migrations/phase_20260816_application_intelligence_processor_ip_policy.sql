-- Govern FPGA soft-CPU and ASIC CPU-IP defaults for Application Intelligence.
-- Supabase is authoritative; runtime code only supplies backward-compatible fallbacks.

do $$
declare
  processor_policy jsonb := jsonb_build_object(
    'schema', 'chiploop.application_intelligence.processor_ip_policy.v2',
    'automatic_asic_deployment', 'asic_digital_ip',
    'fpga_soft_cpu', jsonb_build_object(
      'availability', 'preview',
      'allowed_buses', jsonb_build_array('wishbone','axi4_lite','native'),
      'default_core', 'picorv32',
      'defaults', jsonb_build_object('isa','automatic','bus','automatic','clock_mhz',50,'instruction_memory_kib',32,'data_memory_kib',16,'interrupts',true,'uart',true,'debug',false),
      'cores', jsonb_build_object(
        'serv', jsonb_build_object('label','SERV','license','ISC','profile','minimum_area','default_isa','rv32i','supported_isas',jsonb_build_array('rv32i','rv32im'),'default_bus','wishbone','estimated_logic_cells',900,'estimated_bram_blocks',8),
        'picorv32', jsonb_build_object('label','PicoRV32','license','ISC','profile','balanced','default_isa','rv32imc','supported_isas',jsonb_build_array('rv32i','rv32im','rv32imc'),'default_bus','wishbone','estimated_logic_cells',3000,'estimated_bram_blocks',12),
        'vexriscv', jsonb_build_object('label','VexRiscv','license','MIT','profile','performance','default_isa','rv32imc','supported_isas',jsonb_build_array('rv32i','rv32im','rv32imc'),'default_bus','wishbone','estimated_logic_cells',6000,'estimated_bram_blocks',16)
      )
    ),
    'asic_soc_cpu', jsonb_build_object(
      'availability', 'preview',
      'default_core', 'picorv32',
      'defaults', jsonb_build_object('isa','automatic','bus','automatic','clock_mhz',100,'boot_rom_kib',16,'sram_kib',64,'interrupts',true,'debug',false,'clock_gating',true,'dft_scan_required',true),
      'allowed_buses', jsonb_build_array('apb','axi4_lite','wishbone','native'),
      'cores', jsonb_build_object(
        'serv', jsonb_build_object('label','SERV','license','ISC','profile','minimum_area','default_isa','rv32i','supported_isas',jsonb_build_array('rv32i','rv32im'),'default_bus','apb'),
        'picorv32', jsonb_build_object('label','PicoRV32','license','ISC','profile','balanced','default_isa','rv32imc','supported_isas',jsonb_build_array('rv32i','rv32im','rv32imc'),'default_bus','apb'),
        'vexriscv', jsonb_build_object('label','VexRiscv','license','MIT','profile','performance','default_isa','rv32imc','supported_isas',jsonb_build_array('rv32i','rv32im','rv32imc'),'default_bus','axi4_lite')
      ),
      'integration_gate', jsonb_build_object('cpu_rtl_required',true,'memory_macro_mapping_required',true,'complete_soc_synthesis_required',true,'default_status','pending_cpu_rtl')
    )
  );
begin
  update public.physical_ai_models
  set configuration = coalesce(configuration, '{}'::jsonb) || jsonb_build_object('processor_ip_policy', processor_policy),
      updated_at = now();

  update public.workflows
  set definitions = coalesce(definitions, '{}'::jsonb) || jsonb_build_object('processor_ip_policy', processor_policy, 'schema_version', 4),
      updated_at = now()
  where name = 'Physical_AI_Loop' and user_id is null;
end $$;
