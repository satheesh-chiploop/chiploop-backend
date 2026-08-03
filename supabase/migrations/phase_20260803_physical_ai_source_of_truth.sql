-- Physical AI catalog and parent workflow.
-- Supabase owns discoverable model/workflow metadata. Executable adapters remain
-- versioned backend code; each run snapshots the selected row into its artifacts.

create table if not exists public.physical_ai_models (
  model_id text primary key,
  name text not null,
  provider text not null,
  domain text not null,
  runtime text not null,
  availability text not null check (availability in ('ready', 'requires_gpu_worker', 'disabled')),
  training_required boolean not null default false,
  gpu_required boolean not null default false,
  implementation_targets jsonb not null default '[]'::jsonb,
  executor text,
  inputs jsonb not null default '[]'::jsonb,
  outputs jsonb not null default '[]'::jsonb,
  configuration jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_physical_ai_models_domain_availability
  on public.physical_ai_models (domain, availability);

alter table public.physical_ai_models enable row level security;

drop policy if exists "Authenticated users can read physical AI models" on public.physical_ai_models;
create policy "Authenticated users can read physical AI models"
  on public.physical_ai_models for select
  to authenticated
  using (true);

insert into public.physical_ai_models (
  model_id, name, provider, domain, runtime, availability,
  training_required, gpu_required, implementation_targets, executor, inputs, outputs, configuration
) values
  (
    'chiploop.pmsm.dq.v1', 'PMSM dq Equation Model', 'ChipLoop', 'motor_control',
    'cpu_equation', 'ready', false, false, '["software","fpga","asic"]'::jsonb,
    'pmsm_equation_v1',
    '["dc_bus_voltage_v","rated_speed_rpm","load_torque_nm","control_loop_hz"]'::jsonb,
    '["speed_rpm","id_a","iq_a","torque_nm","winding_temperature_c"]'::jsonb,
    '{"reference_application":"pmsm_motor_control","version":1}'::jsonb
  ),
  (
    'nvidia.domino.automotive_aero', 'NVIDIA DoMINO Automotive Aero', 'NVIDIA',
    'automotive_aerodynamics', 'remote_nim', 'requires_gpu_worker', false, true,
    '["gpu_service"]'::jsonb, null,
    '["vehicle_geometry","flow_conditions"]'::jsonb,
    '["drag_force","lift_force","surface_pressure","flow_field"]'::jsonb,
    '{"version":1}'::jsonb
  )
on conflict (model_id) do update set
  name = excluded.name,
  provider = excluded.provider,
  domain = excluded.domain,
  runtime = excluded.runtime,
  availability = excluded.availability,
  training_required = excluded.training_required,
  gpu_required = excluded.gpu_required,
  implementation_targets = excluded.implementation_targets,
  executor = excluded.executor,
  inputs = excluded.inputs,
  outputs = excluded.outputs,
  configuration = excluded.configuration,
  updated_at = now();

with definition as (
  select jsonb_build_object(
    'nodes', jsonb_build_array(
      jsonb_build_object('id','n1','type','agent','position',jsonb_build_object('x',80,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Requirements Agent','backendLabel','Physical AI Requirements Agent')),
      jsonb_build_object('id','n2','type','agent','position',jsonb_build_object('x',380,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Model Selection Agent','backendLabel','Physical AI Model Selection Agent')),
      jsonb_build_object('id','n3','type','agent','position',jsonb_build_object('x',680,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Physics Execution Agent','backendLabel','Physical AI Physics Execution Agent')),
      jsonb_build_object('id','n4','type','agent','position',jsonb_build_object('x',980,'y',140),'data',jsonb_build_object('uiLabel','Physical AI Orchestrator Agent','backendLabel','Physical AI Orchestrator Agent'))
    ),
    'edges', jsonb_build_array(
      jsonb_build_object('id','e1','source','n1','target','n2'),
      jsonb_build_object('id','e2','source','n2','target','n3'),
      jsonb_build_object('id','e3','source','n3','target','n4')
    ),
    'description', 'Selects a governed physics model, validates it, creates RTL and register-map handoffs, and optionally continues through FPGA, firmware, software validation, and product-demo loops with HEM.',
    'category', 'physical_ai',
    'source_of_truth', 'supabase',
    'schema_version', 1,
    'default_model_id', 'chiploop.pmsm.dq.v1',
    'hem_policy_key', 'physical_ai_fpga_prototype_v1',
    'hem_default_goal', 'product_demo',
    'hardware_execution_requires_approval', true
  ) definitions
), updated as (
  update public.workflows w
  set definitions = d.definitions,
      nodes = d.definitions->'nodes',
      edges = d.definitions->'edges',
      loop_type = 'physical_ai',
      is_prebuilt = true,
      user_id = null,
      status = coalesce(w.status, 'saved'),
      updated_at = now()
  from definition d
  where w.name = 'Physical_AI_Loop' and w.user_id is null
  returning w.id
)
insert into public.workflows(id,user_id,name,loop_type,definitions,nodes,edges,status,is_prebuilt,created_at,updated_at)
select gen_random_uuid(),null,'Physical_AI_Loop','physical_ai',d.definitions,d.definitions->'nodes',d.definitions->'edges','saved',true,now(),now()
from definition d
where not exists (select 1 from updated)
  and not exists (select 1 from public.workflows where name='Physical_AI_Loop' and user_id is null);

comment on table public.physical_ai_models is
  'Supabase source-of-truth catalog for selectable Physical AI equation and surrogate models.';
