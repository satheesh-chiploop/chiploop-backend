-- Add OrangeCrab ECP5-85F as a supported Lattice FPGA board target.
-- Supabase remains the source of truth for prebuilt FPGA workflow/app input contracts.

with board_options as (
  select jsonb_build_array(
    'icebreaker',
    'ice40_hx8k_breakout',
    'ulx3s_ecp5_45f',
    'orangecrab_ecp5_85f',
    'upduino_v3',
    'icestick',
    'custom_ice40'
  ) as options
),
workflow_contracts as (
  select
    w.id,
    jsonb_agg(
      case
        when field.value->>'key' = 'board'
          then jsonb_set(field.value, '{options}', b.options, true)
        else field.value
      end
      order by field.ordinality
    ) as fields
  from public.workflows w
  cross join board_options b
  cross join lateral jsonb_array_elements(coalesce(w.definitions->'input_contract'->'fields', '[]'::jsonb)) with ordinality as field(value, ordinality)
  where w.loop_type = 'fpga'
    and w.user_id is null
    and w.definitions ? 'input_contract'
  group by w.id
),
updated_workflows as (
  update public.workflows w
  set definitions = jsonb_set(
        coalesce(w.definitions, '{}'::jsonb),
        '{input_contract,fields}',
        c.fields,
        true
      ),
      updated_at = now()
  from workflow_contracts c
  where w.id = c.id
  returning w.id
)
select count(*) as orangecrab_workflow_contracts_updated
from updated_workflows;

do $$
begin
  if to_regclass('public.apps') is not null
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'input_contract'
     )
     and exists (
       select 1
       from information_schema.columns
       where table_schema = 'public'
         and table_name = 'apps'
         and column_name = 'updated_at'
     ) then
    with board_options as (
      select jsonb_build_array(
        'icebreaker',
        'ice40_hx8k_breakout',
        'ulx3s_ecp5_45f',
        'orangecrab_ecp5_85f',
        'upduino_v3',
        'icestick',
        'custom_ice40'
      ) as options
    ),
    app_contracts as (
      select
        a.id,
        jsonb_agg(
          case
            when field.value->>'key' = 'board'
              then jsonb_set(field.value, '{options}', b.options, true)
            else field.value
          end
          order by field.ordinality
        ) as fields
      from public.apps a
      cross join board_options b
      cross join lateral jsonb_array_elements(coalesce(a.input_contract->'fields', '[]'::jsonb)) with ordinality as field(value, ordinality)
      where a.input_contract ? 'fields'
        and exists (
          select 1
          from jsonb_array_elements(a.input_contract->'fields') as f(value)
          where f.value->>'key' = 'board'
        )
      group by a.id
    )
    update public.apps a
    set input_contract = jsonb_set(
          coalesce(a.input_contract, '{}'::jsonb),
          '{fields}',
          c.fields,
          true
        ),
        updated_at = now()
    from app_contracts c
    where a.id = c.id;
  end if;
end $$;
