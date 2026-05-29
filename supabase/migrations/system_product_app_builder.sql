-- Publish the shared System Product App Builder Studio workflow.
-- App executions remain user-owned; this only inserts/updates the shared prebuilt template.

with product_builder_definition(definitions) as (
  values (
    jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','Collateral Ingest','backendLabel','System Product Collateral Ingest Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',420,'y',160),'data',jsonb_build_object('uiLabel','Capability Model','backendLabel','System Product Capability Model Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',760,'y',160),'data',jsonb_build_object('uiLabel','Dashboard App','backendLabel','System Product Dashboard Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',1100,'y',160),'data',jsonb_build_object('uiLabel','Product Package','backendLabel','System Product Package Agent'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4')
      )
    )
  )
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(), null, 'System_Product_App_Builder', 'system', definitions, 'saved', true, now(), now()
from product_builder_definition
where not exists (
  select 1 from public.workflows
  where name = 'System_Product_App_Builder' and user_id is null
);

with product_builder_definition(definitions) as (
  values (
    jsonb_build_object(
      'nodes', jsonb_build_array(
        jsonb_build_object('id','n1','type','agentNode','position',jsonb_build_object('x',80,'y',160),'data',jsonb_build_object('uiLabel','Collateral Ingest','backendLabel','System Product Collateral Ingest Agent')),
        jsonb_build_object('id','n2','type','agentNode','position',jsonb_build_object('x',420,'y',160),'data',jsonb_build_object('uiLabel','Capability Model','backendLabel','System Product Capability Model Agent')),
        jsonb_build_object('id','n3','type','agentNode','position',jsonb_build_object('x',760,'y',160),'data',jsonb_build_object('uiLabel','Dashboard App','backendLabel','System Product Dashboard Agent')),
        jsonb_build_object('id','n4','type','agentNode','position',jsonb_build_object('x',1100,'y',160),'data',jsonb_build_object('uiLabel','Product Package','backendLabel','System Product Package Agent'))
      ),
      'edges', jsonb_build_array(
        jsonb_build_object('id','e1','source','n1','target','n2'),
        jsonb_build_object('id','e2','source','n2','target','n3'),
        jsonb_build_object('id','e3','source','n3','target','n4')
      )
    )
  )
)
update public.workflows as workflow
set definitions = product_builder_definition.definitions,
    loop_type = 'system',
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
from product_builder_definition
where workflow.name = 'System_Product_App_Builder'
  and workflow.user_id is null;
