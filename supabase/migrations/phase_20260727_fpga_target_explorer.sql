-- FPGA Target Explorer: compare one RTL design across supported FPGA targets.
-- Supabase remains the workflow source of truth.

with definition as (
  select jsonb_build_object(
    'nodes', jsonb_build_array(
      jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',120,'y',140),'data',jsonb_build_object('uiLabel','FPGA RTL Handoff Ingest Agent','backendLabel','FPGA RTL Handoff Ingest Agent')),
      jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',440,'y',140),'data',jsonb_build_object('uiLabel','FPGA Target Explorer Agent','backendLabel','FPGA Target Explorer Agent'))
    ),
    'edges', jsonb_build_array(jsonb_build_object('id','e1','source','n1','target','n2')),
    'description', 'Runs family-specific Yosys synthesis and controlled nextpnr sweeps across supported FPGA targets, applies synthesis/P&R closure only to target misses, and recommends best overall, performance, low-cost proxy, and growth targets.',
    'category', 'fpga',
    'source_of_truth', 'supabase',
    'input_contract', jsonb_build_object(
      'version', 1,
      'fields', jsonb_build_array(
        jsonb_build_object('key','rtl_source_mode','label','RTL source','type','select','required',true,'defaultValue','from_arch2rtl','options',jsonb_build_array('from_arch2rtl','paste','repo_path')),
        jsonb_build_object('key','from_workflow_id','label','Source workflow ID','type','text','required',false),
        jsonb_build_object('key','source_workflow_id','label','Source workflow ID','type','text','required',false),
        jsonb_build_object('key','repo_path','label','Repo/path','type','text','required',false),
        jsonb_build_object('key','rtl_text','label','RTL text','type','textarea','required',false),
        jsonb_build_object('key','pasted_rtl_files','label','Uploaded RTL files','type','json','required',false),
        jsonb_build_object('key','top_module','label','Top module','type','text','required',false),
        jsonb_build_object('key','target_frequency_mhz','label','Target MHz','type','number','required',true,'defaultValue',75),
        jsonb_build_object('key','recommendation_profile','label','Primary recommendation','type','select','required',false,'defaultValue','best_overall','options',jsonb_build_array('best_overall','best_performance','best_low_cost','best_for_growth')),
        jsonb_build_object('key','candidate_boards','label','Candidate boards','type','json','required',false),
        jsonb_build_object('key','notes','label','Notes','type','textarea','required',false)
      )
    )
  ) definitions
), updated as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = 'fpga',
      is_prebuilt = true,
      user_id = null,
      status = coalesce(w.status, 'saved'),
      updated_at = now()
  from definition d
  where w.name = 'FPGA_Target_Explorer' and w.user_id is null
  returning w.id
)
insert into public.workflows(id,user_id,name,loop_type,definitions,nodes,edges,status,is_prebuilt,created_at,updated_at)
select gen_random_uuid(),null,'FPGA_Target_Explorer','fpga',d.definitions,d.definitions->'nodes',d.definitions->'edges','saved',true,now(),now()
from definition d
where not exists (select 1 from updated)
  and not exists (select 1 from public.workflows where name='FPGA_Target_Explorer' and user_id is null);
