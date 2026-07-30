-- Add explicit per-board I/O mapping evidence before FPGA target exploration.
-- Supabase remains the workflow source of truth.

update public.workflows
set definitions = jsonb_set(
      jsonb_set(
        definitions,
        '{nodes}',
        jsonb_build_array(
          jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',120,'y',140),'data',jsonb_build_object('uiLabel','FPGA RTL Handoff Ingest Agent','backendLabel','FPGA RTL Handoff Ingest Agent')),
          jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',420,'y',140),'data',jsonb_build_object('uiLabel','FPGA RTL Quality Gate Agent','backendLabel','FPGA RTL Quality Gate Agent')),
          jsonb_build_object('id','n3','type','agent','position',jsonb_build_object('x',720,'y',140),'data',jsonb_build_object('uiLabel','FPGA Explorer I/O Mapping Agent','backendLabel','FPGA Explorer I/O Mapping Agent')),
          jsonb_build_object('id','n4','type','agent','position',jsonb_build_object('x',1020,'y',140),'data',jsonb_build_object('uiLabel','FPGA Target Explorer Agent','backendLabel','FPGA Target Explorer Agent'))
        ),
        true
      ),
      '{edges}',
      jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4')
      ),
      true
    ),
    nodes = jsonb_build_array(
      jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',120,'y',140),'data',jsonb_build_object('uiLabel','FPGA RTL Handoff Ingest Agent','backendLabel','FPGA RTL Handoff Ingest Agent')),
      jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',420,'y',140),'data',jsonb_build_object('uiLabel','FPGA RTL Quality Gate Agent','backendLabel','FPGA RTL Quality Gate Agent')),
      jsonb_build_object('id','n3','type','agent','position',jsonb_build_object('x',720,'y',140),'data',jsonb_build_object('uiLabel','FPGA Explorer I/O Mapping Agent','backendLabel','FPGA Explorer I/O Mapping Agent')),
      jsonb_build_object('id','n4','type','agent','position',jsonb_build_object('x',1020,'y',140),'data',jsonb_build_object('uiLabel','FPGA Target Explorer Agent','backendLabel','FPGA Target Explorer Agent'))
    ),
    edges = jsonb_build_array(
      jsonb_build_object('id','e1','source','n1','target','n2'),
      jsonb_build_object('id','e2','source','n2','target','n3'),
      jsonb_build_object('id','e3','source','n3','target','n4')
    ),
    updated_at = now()
where name = 'FPGA_Target_Explorer' and user_id is null;