-- Products surface: user-owned products plus public reference journey templates.

create table if not exists public.products (
  id uuid primary key default gen_random_uuid(),
  user_id uuid null,
  name text not null,
  product_type text not null default 'digital',
  starting_point text not null default 'from_specs',
  description text not null default '',
  stage_config jsonb not null default '{}'::jsonb,
  stage_results jsonb not null default '{}'::jsonb,
  status text not null default 'draft',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_products_user_updated
  on public.products (user_id, updated_at desc);

create table if not exists public.product_reference_journeys (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  product_type text not null default 'digital',
  summary text not null default '',
  definition jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_product_reference_journeys_active
  on public.product_reference_journeys (is_active, product_type, name);

alter table public.products enable row level security;
alter table public.product_reference_journeys enable row level security;

drop policy if exists "Users can read own products" on public.products;
create policy "Users can read own products"
  on public.products
  for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert own products" on public.products;
create policy "Users can insert own products"
  on public.products
  for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update own products" on public.products;
create policy "Users can update own products"
  on public.products
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Public can read active reference journeys" on public.product_reference_journeys;
create policy "Public can read active reference journeys"
  on public.product_reference_journeys
  for select
  using (is_active = true);

with refs(slug, name, product_type, summary, stages) as (
  values
    (
      'pwm-fan-controller',
      'PWM Fan Controller',
      'digital',
      'Full development from PWM RTL through verification, firmware, software validation, and simulator-backed product app.',
      '[
        {"id":"arch2rtl","label":"RTL","app":"Digital_Arch2RTL","required":true},
        {"id":"dqa","label":"DQA","app":"Digital_DQA","required":true},
        {"id":"verify","label":"Verification","app":"Digital_Verify","required":true},
        {"id":"closure","label":"Closure","app":"verify_closure_loop","recommended":true},
        {"id":"firmware","label":"Firmware","app":"Embedded_Run","recommended":true},
        {"id":"software","label":"Software","app":"System_Software","recommended":true},
        {"id":"validation","label":"Validation","app":"System_Software_Validation_L2","recommended":true},
        {"id":"product_app","label":"Product App","app":"System_Product_App_Builder","recommended":true}
      ]'::jsonb
    ),
    (
      'uart-packet-engine',
      'UART Packet Engine',
      'digital',
      'UART/FIFO/interrupt journey from Arch2RTL to verification, firmware, software validation, and product app.',
      '[
        {"id":"arch2rtl","label":"RTL","app":"Digital_Arch2RTL","required":true},
        {"id":"dqa","label":"DQA","app":"Digital_DQA","required":true},
        {"id":"verify","label":"Verification","app":"Digital_Verify","required":true},
        {"id":"closure","label":"Closure","app":"verify_closure_loop","recommended":true},
        {"id":"firmware","label":"Firmware","app":"Embedded_Run","recommended":true},
        {"id":"software","label":"Software","app":"System_Software","recommended":true},
        {"id":"product_app","label":"Product App","app":"System_Product_App_Builder","recommended":true}
      ]'::jsonb
    ),
    (
      'image-dma-pipeline',
      'Image DMA Pipeline',
      'digital',
      'Large image-processing journey with DMA, line buffers, verification, firmware/software validation, and product dashboard.',
      '[
        {"id":"arch2rtl","label":"RTL","app":"Digital_Arch2RTL","required":true},
        {"id":"dqa","label":"DQA","app":"Digital_DQA","required":true},
        {"id":"verify","label":"Verification","app":"Digital_Verify","required":true},
        {"id":"synthesis","label":"Synthesis","app":"Digital_Arch2Synthesis","recommended":true},
        {"id":"firmware","label":"Firmware","app":"Embedded_Run","recommended":true},
        {"id":"validation","label":"Validation","app":"System_Software_Validation_L2","recommended":true},
        {"id":"product_app","label":"Product App","app":"System_Product_App_Builder","recommended":true}
      ]'::jsonb
    ),
    (
      'digital-synthesis-reference',
      'Digital Synthesis Reference',
      'digital',
      'Digital product journey from Arch2RTL through DQA, synthesis readiness, verification, firmware/software, and product app handoff.',
      '[
        {"id":"arch2rtl","label":"RTL","app":"Digital_Arch2RTL","required":true},
        {"id":"dqa","label":"DQA","app":"Digital_DQA","required":true},
        {"id":"synthesis","label":"Synthesis","app":"Digital_Arch2Synthesis","recommended":true},
        {"id":"verify","label":"Verification","app":"Digital_Verify","recommended":true},
        {"id":"firmware","label":"Firmware","app":"Embedded_Run","optional":true},
        {"id":"software","label":"Software","app":"System_Software","optional":true},
        {"id":"product_app","label":"Product App","app":"System_Product_App_Builder","optional":true}
      ]'::jsonb
    ),
    (
      'temperature-monitor-soc',
      'Temperature Monitor SoC',
      'mixed_signal',
      'Mixed-signal System RTL journey with digital, analog, SoC specs, System Sim, firmware, software, validation, and product app.',
      '[
        {"id":"system_rtl","label":"System RTL","app":"System_RTL","required":true},
        {"id":"system_dqa","label":"System DQA","app":"System_DQA","required":true},
        {"id":"system_sim","label":"System Sim","app":"System_Sim","required":true},
        {"id":"system_firmware","label":"Firmware","app":"System_Firmware","recommended":true},
        {"id":"system_software","label":"Software","app":"System_Software","recommended":true},
        {"id":"system_validation","label":"Validation","app":"System_Software_Validation_L2","recommended":true},
        {"id":"system_pd","label":"System PD","app":"System_PD","optional":true},
        {"id":"product_app","label":"Product App","app":"System_Product_App_Builder","recommended":true}
      ]'::jsonb
    )
)
insert into public.product_reference_journeys (slug, name, product_type, summary, definition, is_active, updated_at)
select
  slug,
  name,
  product_type,
  summary,
  jsonb_build_object('stages', stages),
  true,
  now()
from refs
on conflict (slug) do update
set
  name = excluded.name,
  product_type = excluded.product_type,
  summary = excluded.summary,
  definition = excluded.definition,
  is_active = true,
  updated_at = now();
