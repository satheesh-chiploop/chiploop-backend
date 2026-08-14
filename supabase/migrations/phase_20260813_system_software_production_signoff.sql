-- Align Supabase-owned prebuilt Software workflows with the production runtime.
-- Execution rows are not touched; only shared templates (user_id is null).
with template_agents(template_name, agents) as (
  values
    ('System_Software', array[
      'System Software Handoff Ingest Agent',
      'System Software Capability Model Agent',
      'System Software API Contract Agent',
      'System Software SDK Scaffold Agent',
      'System Software HAL/Driver Adapter Agent',
      'System Software Config Schema Agent',
      'System Software Service Architecture Agent',
      'System Software Core Service Agent',
      'System Software Application Scaffold Agent',
      'System Software Build System Agent',
      'System Software Unit Test Agent',
      'System Software Mock Runtime Agent',
      'System Software Packaging Agent',
      'System Software Executive Summary Agent'
    ]::text[]),
    ('System_Software_Validation_L2', array[
      'System Software Validation Ingest Agent',
      'System Software Build Validation Agent',
      'System Software Test Execution Agent',
      'System Software Contract Consistency Agent',
      'System Software Mock Runtime Validation Agent',
      'System Software Package Audit Agent',
      'System CoSim Ingest Agent',
      'System CoSim Contract Agent',
      'System CoSim Scenario Generator Agent',
      'System Software CoSim Harness Agent',
      'System Software CoSim Execution Agent',
      'System Software CoSim Trace Validation Agent',
      'System Software Validation Summary (L2)'
    ]::text[])
),
expanded as (
  select template_name, agent_name, ordinal::integer as ordinal
  from template_agents
  cross join lateral unnest(agents) with ordinality as item(agent_name, ordinal)
),
nodes as (
  select template_name,
         jsonb_agg(jsonb_build_object(
           'id', 'n' || ordinal,
           'type', 'agentNode',
           'position', jsonb_build_object(
             'x', 80 + (((ordinal - 1) % 5) * 260),
             'y', 160 + (((ordinal - 1) / 5) * 220)
           ),
           'data', jsonb_build_object('uiLabel', agent_name, 'backendLabel', agent_name)
         ) order by ordinal) as value
  from expanded
  group by template_name
),
edges as (
  select template_name,
         jsonb_agg(jsonb_build_object(
           'id', 'e' || (ordinal - 1),
           'source', 'n' || (ordinal - 1),
           'target', 'n' || ordinal
         ) order by ordinal) as value
  from expanded
  where ordinal > 1
  group by template_name
)
update public.workflows as workflow
set definitions = jsonb_build_object('nodes', nodes.value, 'edges', edges.value),
    is_prebuilt = true,
    updated_at = now()
from nodes
join edges using (template_name)
where workflow.name = nodes.template_name
  and workflow.user_id is null;
