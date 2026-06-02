-- Publish ChipLoop-owned workflow templates to all Studio users.
--
-- Important: App executions are also stored in public.workflows. Those rows
-- contain user logs/artifacts and must remain private. Only global template
-- rows (user_id is null) with an approved platform template name are promoted.

alter table if exists public.workflows
  add column if not exists is_prebuilt boolean not null default false;

-- The focused System Architecture apps were initially routed through the
-- generic Explorer template only. Insert named Studio templates for them so
-- each App is visible as its own prebuilt workflow.
with focused_system_templates(name) as (
  values
    ('System_Cache_Tuning'),
    ('System_ISA_Compare'),
    ('System_Memory_Bottleneck'),
    ('System_CPU_Model')
),
gem5_definition(definitions) as (
  values (
    '{
      "nodes": [
        {"id": "n1", "type": "agentNode", "position": {"x": 80, "y": 160}, "data": {"uiLabel": "Architecture Intent Agent", "backendLabel": "System Architecture Intent Agent"}},
        {"id": "n2", "type": "agentNode", "position": {"x": 452, "y": 160}, "data": {"uiLabel": "Workload Characterization Agent", "backendLabel": "System Workload Characterization Agent"}},
        {"id": "n3", "type": "agentNode", "position": {"x": 824, "y": 160}, "data": {"uiLabel": "gem5 Config Agent", "backendLabel": "System gem5 Config Agent"}},
        {"id": "n4", "type": "agentNode", "position": {"x": 1196, "y": 160}, "data": {"uiLabel": "Design Space Exploration Agent", "backendLabel": "System Design Space Exploration Agent"}},
        {"id": "n5", "type": "agentNode", "position": {"x": 80, "y": 370}, "data": {"uiLabel": "gem5 Execution Agent", "backendLabel": "System gem5 Execution Agent"}},
        {"id": "n6", "type": "agentNode", "position": {"x": 452, "y": 370}, "data": {"uiLabel": "Performance Metrics Agent", "backendLabel": "System Performance Metrics Agent"}},
        {"id": "n7", "type": "agentNode", "position": {"x": 824, "y": 370}, "data": {"uiLabel": "Power Estimation Agent", "backendLabel": "System Power Estimation Agent"}},
        {"id": "n8", "type": "agentNode", "position": {"x": 1196, "y": 370}, "data": {"uiLabel": "Area Estimation Agent", "backendLabel": "System Area Estimation Agent"}},
        {"id": "n9", "type": "agentNode", "position": {"x": 80, "y": 580}, "data": {"uiLabel": "PPA Tradeoff Agent", "backendLabel": "System PPA Tradeoff Agent"}},
        {"id": "n10", "type": "agentNode", "position": {"x": 452, "y": 580}, "data": {"uiLabel": "Visualization Agent", "backendLabel": "System Visualization Agent"}},
        {"id": "n11", "type": "agentNode", "position": {"x": 824, "y": 580}, "data": {"uiLabel": "Architecture Report Agent", "backendLabel": "System Architecture Report Agent"}}
      ],
      "edges": [
        {"id": "e1", "source": "n1", "target": "n2"},
        {"id": "e2", "source": "n2", "target": "n3"},
        {"id": "e3", "source": "n3", "target": "n4"},
        {"id": "e4", "source": "n4", "target": "n5"},
        {"id": "e5", "source": "n5", "target": "n6"},
        {"id": "e6", "source": "n6", "target": "n7"},
        {"id": "e7", "source": "n7", "target": "n8"},
        {"id": "e8", "source": "n8", "target": "n9"},
        {"id": "e9", "source": "n9", "target": "n10"},
        {"id": "e10", "source": "n10", "target": "n11"}
      ]
    }'::jsonb
  )
)
insert into public.workflows (
  id,
  user_id,
  name,
  loop_type,
  definitions,
  status,
  is_prebuilt,
  created_at,
  updated_at
)
select
  gen_random_uuid(),
  null,
  template.name,
  'system',
  definition.definitions,
  'saved',
  true,
  now(),
  now()
from focused_system_templates as template
cross join gem5_definition as definition
where not exists (
  select 1
  from public.workflows existing
  where existing.name = template.name
    and existing.user_id is null
);

with platform_templates(name) as (
  values
    ('Digital_Arch2RTL'),
    ('Digital_Arch2Sim'),
    ('Digital_Arch2Synthesis'),
    ('Digital_Arch2Tapeout'),
    ('Digital_DQA'),
    ('Digital_Verify'),
    ('Digital_Smoke'),
    ('Digital_Integrate'),
    ('Analog_Run'),
    ('Analog_SpecBuilder'),
    ('Analog_NetlistScaffold'),
    ('Analog_BehavioralModel'),
    ('Analog_ModelValidation'),
    ('Analog_Correlation'),
    ('Analog_Iterate'),
    ('Analog_Abstracts'),
    ('Embedded_Run'),
    ('Embedded_HAL'),
    ('Embedded_Driver'),
    ('Embedded_Boot'),
    ('Embedded_Diagnostics'),
    ('Embedded_LogAnalyzer'),
    ('Embedded_Validate'),
    ('System_End2End'),
    ('System_Architecture_Explorer'),
    ('System_Cache_Tuning'),
    ('System_ISA_Compare'),
    ('System_Memory_Bottleneck'),
    ('System_CPU_Model'),
    ('System_Architecture_to_RTL_Delivery'),
    ('System_Sim'),
    ('System_RTL'),
    ('System_PD'),
    ('System_Firmware'),
    ('System_Software'),
    ('System_Software_Validation_L1'),
    ('System_Software_Validation_L2'),
    ('Validation_Run'),
    ('Validation_PlanCoverage'),
    ('Validation_BenchSetup'),
    ('Validation_Preflight'),
    ('Validation_Insights')
)
update public.workflows as workflow
set is_prebuilt = true,
    updated_at = now()
from platform_templates as template
where workflow.name = template.name
  and workflow.user_id is null
  and coalesce(workflow.is_prebuilt, false) = false;

create index if not exists idx_workflows_prebuilt_name
  on public.workflows (name)
  where is_prebuilt = true;

-- If RLS is already enabled for workflows, allow the frontend to list/read
-- shared templates. This deliberately does not enable RLS or change access to
-- private workflow executions.
do $$
begin
  if exists (
    select 1
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relname = 'workflows'
      and c.relrowsecurity = true
  ) and not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'workflows'
      and policyname = 'Shared prebuilt workflows are readable'
  ) then
    execute 'create policy "Shared prebuilt workflows are readable"
      on public.workflows
      for select
      to anon, authenticated
      using (is_prebuilt = true)';
  end if;
end
$$;

-- Verification query to run after applying this migration:
-- select name, is_prebuilt, user_id
-- from public.workflows
-- where is_prebuilt = true
-- order by loop_type, name;
