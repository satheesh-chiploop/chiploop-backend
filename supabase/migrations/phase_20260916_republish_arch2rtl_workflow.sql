-- Supabase is the runtime source of truth. Republish the complete canonical
-- Arch2RTL graph so stale platform rows cannot begin at RTL or packaging.
with workflow_template as (
  select 'Digital_Arch2RTL'::text as name, 'digital'::text as loop_type, array[
    'Digital Spec Agent',
    'Digital Architecture Agent',
    'Digital Microarchitecture Agent',
    'Digital Register Map Agent',
    'Digital RTL Agent',
    'Digital MBIST RTL Insertion Agent',
    'Digital Power Intent (UPF-lite) Agent',
    'Digital UPF Static Check Agent',
    'Digital IP Packaging & Handoff Agent',
    'Digital Arch2RTL Dashboard Agent'
  ]::text[] as agents
),
expanded as (
  select
    template.name,
    template.loop_type,
    jsonb_agg(
      jsonb_build_object(
        'id', 'n' || ord::text,
        'type', 'agentNode',
        'position', jsonb_build_object(
          'x', 80 + (((ord - 1) % 4) * 372),
          'y', 160 + (((ord - 1) / 4) * 210)
        ),
        'data', jsonb_build_object(
          'desc', 'Auto-composed: ' || agent,
          'uiLabel', agent,
          'backendLabel', agent
        )
      ) order by ord
    ) as nodes,
    jsonb_agg(
      jsonb_build_object(
        'id', 'e' || (ord - 1)::text,
        'source', 'n' || (ord - 1)::text,
        'target', 'n' || ord::text
      ) order by ord
    ) filter (where ord > 1) as edges
  from workflow_template template
  cross join lateral unnest(template.agents) with ordinality as item(agent, ord)
  group by template.name, template.loop_type
),
updated as (
  update public.workflows workflow
  set
    loop_type = expanded.loop_type,
    definitions = jsonb_build_object('nodes', expanded.nodes, 'edges', coalesce(expanded.edges, '[]'::jsonb)),
    nodes = expanded.nodes,
    edges = coalesce(expanded.edges, '[]'::jsonb),
    status = 'saved',
    is_prebuilt = true,
    updated_at = now()
  from expanded
  where workflow.name = expanded.name
    and workflow.user_id is null
  returning workflow.id
)
insert into public.workflows (
  id, user_id, name, loop_type, definitions, nodes, edges,
  status, is_prebuilt, created_at, updated_at
)
select
  gen_random_uuid(), null, expanded.name, expanded.loop_type,
  jsonb_build_object('nodes', expanded.nodes, 'edges', coalesce(expanded.edges, '[]'::jsonb)),
  expanded.nodes, coalesce(expanded.edges, '[]'::jsonb),
  'saved', true, now(), now()
from expanded
where not exists (select 1 from updated)
  and not exists (
    select 1 from public.workflows workflow
    where workflow.name = expanded.name and workflow.user_id is null
  );
