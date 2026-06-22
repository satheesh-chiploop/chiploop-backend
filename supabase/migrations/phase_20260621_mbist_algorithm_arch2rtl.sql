create table if not exists public.product_stage_schemas (
  id uuid primary key default gen_random_uuid(),
  app text not null unique,
  schema jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into public.product_stage_schemas (app, schema, is_active)
select
  'Digital_Arch2RTL',
  '{"fields":[{"key":"mbist_algorithm","label":"MBIST algorithm","type":"select","defaultValue":"march-c","options":["march-c","march-raw"]}]}'::jsonb,
  true
where not exists (
  select 1 from public.product_stage_schemas where app = 'Digital_Arch2RTL'
);

update public.product_stage_schemas
set
  schema = jsonb_set(
    schema,
    '{fields}',
    coalesce(schema->'fields', '[]'::jsonb) ||
      '[{"key":"mbist_algorithm","label":"MBIST algorithm","type":"select","defaultValue":"march-c","options":["march-c","march-raw"]}]'::jsonb
  ),
  updated_at = now()
where app = 'Digital_Arch2RTL'
  and not exists (
    select 1
    from jsonb_array_elements(coalesce(schema->'fields', '[]'::jsonb)) field
    where field->>'key' = 'mbist_algorithm'
  );
