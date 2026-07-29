-- Open-source-only Lattice and Gowin target catalog for every FPGA workflow/app.
-- Labels are vendor-prefixed; unavailable growth targets remain catalogued but cannot be selected for execution.

with target_catalog as (
  select jsonb_build_array(
    jsonb_build_object('key','icebreaker','label','Lattice iCEBreaker','vendor','Lattice','family','iCE40','tier','production','segments',jsonb_build_array('education','IoT','embedded control')),
    jsonb_build_object('key','upduino_v3','label','Lattice UPduino v3','vendor','Lattice','family','iCE40','tier','production','segments',jsonb_build_array('makers','IoT','low-cost embedded')),
    jsonb_build_object('key','icestick','label','Lattice iCEstick','vendor','Lattice','family','iCE40','tier','production','segments',jsonb_build_array('education','small control')),
    jsonb_build_object('key','ice40_hx8k_breakout','label','Lattice iCE40 HX8K Breakout','vendor','Lattice','family','iCE40','tier','production','segments',jsonb_build_array('general prototyping')),
    jsonb_build_object('key','colorlight_5a_75b','label','Lattice Colorlight 5A-75B ECP5-25F','vendor','Lattice','family','ECP5','tier','production','segments',jsonb_build_array('display','video','networking')),
    jsonb_build_object('key','ulx3s_ecp5_45f','label','Lattice ULX3S ECP5-45F','vendor','Lattice','family','ECP5','tier','production','segments',jsonb_build_array('video','soft CPUs','general prototyping')),
    jsonb_build_object('key','orangecrab_ecp5_85f','label','Lattice OrangeCrab ECP5-85F','vendor','Lattice','family','ECP5','tier','production','segments',jsonb_build_array('compute','networking','growth')),
    jsonb_build_object('key','certus_nx_versa_40','label','Lattice Certus-NX Versa LFD2NX-40','vendor','Lattice','family','Certus-NX','tier','experimental','segments',jsonb_build_array('industrial','embedded','connectivity')),
    jsonb_build_object('key','crosslink_nx_eval_40','label','Lattice CrossLink-NX Evaluation Board LIFCL-40','vendor','Lattice','family','CrossLink-NX','tier','experimental','segments',jsonb_build_array('machine vision','camera/display bridging')),
    jsonb_build_object('key','certuspro_nx_versa_100','label','Lattice CertusPro-NX Versa LFCPNX-100','vendor','Lattice','family','CertusPro-NX','tier','unavailable','segments',jsonb_build_array('networking','infrastructure','acceleration')),
    jsonb_build_object('key','machxo5_nx_65t','label','Lattice MachXO5-NX 65T Development Board','vendor','Lattice','family','MachXO5-NX','tier','unavailable','segments',jsonb_build_array('secure control','platform management')),
    jsonb_build_object('key','gowin_tang_nano_9k','label','Gowin Tang Nano 9K LittleBee GW1NR-9','vendor','Gowin','family','LittleBee','tier','beta','segments',jsonb_build_array('education','IoT','low-cost embedded')),
    jsonb_build_object('key','gowin_tang_nano_20k','label','Gowin Tang Nano 20K Arora II GW2AR-18C','vendor','Gowin','family','Arora II','tier','beta','segments',jsonb_build_array('video','DSP','robotics','industrial')),
    jsonb_build_object('key','gowin_tang_primer_20k','label','Gowin Tang Primer 20K Arora II GW2A-18','vendor','Gowin','family','Arora II','tier','beta','segments',jsonb_build_array('motor control','embedded compute','communications')),
    jsonb_build_object('key','gowin_gw5a_25_starter','label','Gowin Arora V GW5A-25 Starter Board','vendor','Gowin','family','Arora V','tier','unavailable','segments',jsonb_build_array('machine vision','display','DSP','edge processing')),
    jsonb_build_object('key','gowin_gw5at_60_pcie','label','Gowin Arora V GW5AT-60 PCIe Board','vendor','Gowin','family','Arora V','tier','unavailable','segments',jsonb_build_array('PCIe','SerDes','networking')),
    jsonb_build_object('key','gowin_gw5ast_138','label','Gowin Arora V GW5AST-138 RISC-V Board','vendor','Gowin','family','Arora V','tier','unavailable','segments',jsonb_build_array('RISC-V','edge AI','industrial compute')),
    jsonb_build_object('key','gowin_gw3a_20k','label','Gowin Arora III GW3A-20K Starter Board','vendor','Gowin','family','Arora III','tier','unavailable','segments',jsonb_build_array('industrial','vision','display','DSP'))
  ) as catalog
), runnable as (
  select jsonb_agg(item->>'key' order by ordinality) as options
  from target_catalog, jsonb_array_elements(catalog) with ordinality as entry(item, ordinality)
  where item->>'tier' <> 'unavailable'
), workflow_contracts as (
  select w.id, c.catalog,
    jsonb_agg(case when field.value->>'key' = 'board' then jsonb_set(field.value,'{options}',r.options,true) else field.value end order by field.ordinality) as fields
  from public.workflows w cross join target_catalog c cross join runnable r
  cross join lateral jsonb_array_elements(coalesce(w.definitions->'input_contract'->'fields','[]'::jsonb)) with ordinality as field(value,ordinality)
  where w.loop_type='fpga' and w.user_id is null and w.definitions ? 'input_contract'
  group by w.id,c.catalog
)
update public.workflows w
set definitions=jsonb_set(jsonb_set(coalesce(w.definitions,'{}'::jsonb),'{fpga_target_catalog}',c.catalog,true),'{input_contract,fields}',c.fields,true), updated_at=now()
from workflow_contracts c where w.id=c.id;

do $$
begin
  if to_regclass('public.apps') is not null and exists(select 1 from information_schema.columns where table_schema='public' and table_name='apps' and column_name='input_contract') then
    with target_catalog as (
      select definitions->'fpga_target_catalog' as catalog from public.workflows where name='FPGA_Target_Explorer' and user_id is null limit 1
    ), runnable as (
      select jsonb_agg(item->>'key' order by ordinality) options from target_catalog, jsonb_array_elements(catalog) with ordinality as entry(item,ordinality) where item->>'tier'<>'unavailable'
    ), contracts as (
      select a.id,c.catalog,jsonb_agg(case when field.value->>'key'='board' then jsonb_set(field.value,'{options}',r.options,true) else field.value end order by field.ordinality) fields
      from public.apps a cross join target_catalog c cross join runnable r
      cross join lateral jsonb_array_elements(coalesce(a.input_contract->'fields','[]'::jsonb)) with ordinality as field(value,ordinality)
      group by a.id,c.catalog
    )
    update public.apps a set input_contract=jsonb_set(jsonb_set(coalesce(a.input_contract,'{}'::jsonb),'{fpga_target_catalog}',c.catalog,true),'{fields}',c.fields,true),updated_at=now()
    from contracts c where a.id=c.id;
  end if;
end $$;
