-- Add opt-in synthesis closure tool options without overwriting existing schemas.
-- Defaults stay false: no retiming and no hierarchy flattening unless the user checks them.

with targets(app) as (
  values
    ('Digital_Arch2Synthesis'),
    ('Digital_Arch2Tapeout'),
    ('System_Synthesis'),
    ('System_PD')
),
new_fields(fields) as (
  values (
    '[
      {"key":"allow_synthesis_retiming","label":"Allow synthesis retiming","type":"boolean","defaultValue":false},
      {"key":"allow_synthesis_hierarchy_flattening","label":"Allow synthesis hierarchy flattening","type":"boolean","defaultValue":false}
    ]'::jsonb
  )
)
update public.product_stage_schemas p
set schema = jsonb_set(
      coalesce(p.schema, '{}'::jsonb),
      '{fields}',
      coalesce(p.schema->'fields', '[]'::jsonb) || coalesce((
        select jsonb_agg(field)
        from jsonb_array_elements((select fields from new_fields)) as field
        where not exists (
          select 1
          from jsonb_array_elements(coalesce(p.schema->'fields', '[]'::jsonb)) as existing
          where existing->>'key' = field->>'key'
        )
      ), '[]'::jsonb),
      true
    ),
    updated_at = now()
from targets
where p.app = targets.app;
