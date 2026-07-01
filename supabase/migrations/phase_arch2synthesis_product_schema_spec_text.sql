-- Add the user-facing digital spec input to Arch2Synthesis Product configuration.
-- This is needed for private Apps created from Digital_Arch2Synthesis workflows
-- that run the full workflow without an upstream Arch2RTL stage in the Product.

create table if not exists public.product_stage_schemas (
  app text primary key,
  schema jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  updated_at timestamptz not null default now()
);

with existing as (
  select
    coalesce(schema->'fields', '[]'::jsonb) as fields
  from public.product_stage_schemas
  where app = 'Digital_Arch2Synthesis'
),
cleaned as (
  select coalesce(
    jsonb_agg(field) filter (
      where field->>'key' not in ('spec_text', 'top_module')
    ),
    '[]'::jsonb
  ) as fields
  from existing, jsonb_array_elements(existing.fields) as field
),
next_schema as (
  select jsonb_build_object(
    'note',
    'Synthesis uses an upstream Arch2RTL handoff when available. For an Arch2Synthesis private app that runs the full workflow, provide the digital spec text here.',
    'fields',
    jsonb_build_array(
      jsonb_build_object(
        'key', 'spec_text',
        'label', 'Digital spec text',
        'type', 'textarea',
        'defaultValue', '',
        'helper', 'Required when this app is not using an upstream Arch2RTL handoff.'
      ),
      jsonb_build_object(
        'key', 'top_module',
        'label', 'Top module',
        'type', 'text',
        'defaultValue', ''
      )
    ) || coalesce((select fields from cleaned), '[]'::jsonb)
  ) as schema
)
insert into public.product_stage_schemas (app, schema, is_active, updated_at)
select 'Digital_Arch2Synthesis', schema, true, now()
from next_schema
on conflict (app) do update
set schema = excluded.schema,
    is_active = true,
    updated_at = now();
