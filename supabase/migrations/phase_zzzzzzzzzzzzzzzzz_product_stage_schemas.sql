-- Product stage configuration schemas.
-- These drive the Products Configure step; backend constants are only fallback.

create table if not exists public.product_stage_schemas (
  id uuid primary key default gen_random_uuid(),
  app text not null unique,
  schema jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_product_stage_schemas_active
  on public.product_stage_schemas (is_active, app);

alter table public.product_stage_schemas enable row level security;

drop policy if exists "Public can read active product stage schemas" on public.product_stage_schemas;
create policy "Public can read active product stage schemas"
  on public.product_stage_schemas
  for select
  using (is_active = true);

with schemas(app, schema) as (
  values
    (
      'Digital_Arch2RTL',
      '{
        "note": "Spec text can be left blank only when the product description is detailed enough to use as the RTL spec.",
        "fields": [
          {"key":"top_module","label":"Top module","type":"text","defaultValue":""},
          {"key":"spec_text","label":"Spec text","type":"text","defaultValue":"","helper":"Used before product description fallback."},
          {"key":"enable_packaging","label":"Generate handoff package","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'Digital_Verify',
      '{
        "fields": [
          {"key":"seed_count","label":"Seed count","type":"number","defaultValue":4},
          {"key":"coverage_targets","label":"Coverage target","type":"text","defaultValue":"90% functional, 70% line"},
          {"key":"enable_formal","label":"Formal","type":"boolean","defaultValue":false}
        ]
      }'::jsonb
    ),
    (
      'Digital_Arch2Synthesis',
      '{
        "note": "Synthesis uses the generated Arch2RTL handoff as RTL input and runs the synthesis stage directly.",
        "fields": [
          {"key":"foundry","label":"Foundry","type":"text","defaultValue":"sky130"},
          {"key":"pdk","label":"PDK","type":"text","defaultValue":"sky130A"},
          {"key":"toolchain","label":"Toolchain","type":"text","defaultValue":"openlane2"},
          {"key":"target_frequency_mhz","label":"Target frequency MHz","type":"number","defaultValue":100},
          {"key":"constraints_sdc","label":"Constraints SDC","type":"text","defaultValue":""}
        ]
      }'::jsonb
    ),
    (
      'verify_closure_loop',
      '{
        "fields": [
          {"key":"max_iterations","label":"Max iterations","type":"number","defaultValue":1},
          {"key":"seed_count","label":"Seed count","type":"number","defaultValue":5},
          {"key":"coverage_targets","label":"Coverage target","type":"text","defaultValue":"90% functional, 70% line"}
        ]
      }'::jsonb
    ),
    (
      'Embedded_Run',
      '{
        "fields": [
          {"key":"firmware_language","label":"Firmware language","type":"text","defaultValue":"rust"},
          {"key":"enable_cosim","label":"Enable firmware co-sim","type":"boolean","defaultValue":false}
        ]
      }'::jsonb
    ),
    (
      'System_RTL',
      '{
        "note": "System RTL starts from explicit digital, analog, and SoC specs. Downstream System apps auto-bind to this generated workflow.",
        "fields": [
          {"key":"digital_spec","label":"Digital spec","type":"text","defaultValue":"","required":true},
          {"key":"analog_spec","label":"Analog spec","type":"text","defaultValue":"","required":true},
          {"key":"soc_spec","label":"SoC spec","type":"text","defaultValue":"","required":true},
          {"key":"enable_spec2rtl","label":"Spec2RTL check","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'System_DQA',
      '{
        "note": "System DQA uses the System RTL handoff and does not rerun register-map generation.",
        "fields": [
          {"key":"run_lint","label":"Run lint","type":"boolean","defaultValue":true},
          {"key":"run_cdc","label":"Run CDC","type":"boolean","defaultValue":true},
          {"key":"run_reset","label":"Run reset integrity","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'System_Sim',
      '{
        "fields": [
          {"key":"seed_count","label":"Seed count","type":"number","defaultValue":6},
          {"key":"system_sim_testcases","label":"Testcases","type":"text","defaultValue":"system_smoke_test, integrated_input_sanity"},
          {"key":"system_sim_seeds","label":"Seeds","type":"text","defaultValue":"1,2,3,4"},
          {"key":"coverage_targets","label":"Coverage target","type":"text","defaultValue":"90% functional"},
          {"key":"enable_golden_model","label":"Golden model","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'System_Firmware',
      '{
        "note": "Firmware auto-binds the System RTL workflow ID, including register-map and top-level handoff artifacts.",
        "fields": [
          {"key":"firmware_language","label":"Firmware language","type":"text","defaultValue":"rust"},
          {"key":"validate_registers","label":"Validate registers","type":"boolean","defaultValue":true},
          {"key":"enable_cosim","label":"Enable firmware co-sim","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'System_PD',
      '{
        "fields": [
          {"key":"foundry","label":"Foundry","type":"text","defaultValue":"sky130"},
          {"key":"pdk","label":"PDK","type":"text","defaultValue":"sky130"},
          {"key":"analog_physical_mode","label":"Analog physical mode","type":"text","defaultValue":"blackbox"},
          {"key":"run_drc","label":"Run DRC","type":"boolean","defaultValue":true},
          {"key":"run_lvs","label":"Run LVS","type":"boolean","defaultValue":true}
        ]
      }'::jsonb
    ),
    (
      'System_Software',
      '{
        "fields": [
          {"key":"app_names","label":"App names","type":"text","defaultValue":"status_cli, product_service"},
          {"key":"target_language","label":"Target language","type":"text","defaultValue":"rust"}
        ]
      }'::jsonb
    ),
    (
      'System_Software_Validation_L2',
      '{
        "fields": [
          {"key":"validation_mode","label":"Validation mode","type":"text","defaultValue":"full_co_simulation"}
        ]
      }'::jsonb
    ),
    (
      'System_Product_App_Builder',
      '{
        "fields": [
          {"key":"app_type","label":"App type","type":"text","defaultValue":"web_dashboard"},
          {"key":"target_runtime","label":"Target runtime","type":"text","defaultValue":"simulated_device"}
        ]
      }'::jsonb
    )
)
insert into public.product_stage_schemas (app, schema, is_active, updated_at)
select app, schema, true, now()
from schemas
on conflict (app) do update
set
  schema = excluded.schema,
  is_active = true,
  updated_at = now();
