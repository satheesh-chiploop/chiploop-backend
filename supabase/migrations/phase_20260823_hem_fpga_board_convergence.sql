-- Authoritative HEM FPGA board-convergence ledger.
-- Each row represents one Explorer selection followed by board-specific
-- integration. HEM never uses local files as the retry source of truth.

create table if not exists public.hem_fpga_board_attempts (
  id uuid primary key default gen_random_uuid(),
  hem_run_id uuid not null references public.hem_runs(id) on delete cascade,
  user_id text not null,
  attempt_number integer not null check (attempt_number between 1 and 10),
  board_id text not null,
  deployment_architecture text not null,
  explorer_workflow_id text not null,
  integration_workflow_id text,
  implementation_workflow_id text,
  status text not null default 'selected' check (
    status in ('selected', 'fit_verified', 'fit_failed', 'integration_failed')
  ),
  failure_class text,
  failure_reason text,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (hem_run_id, attempt_number),
  unique (hem_run_id, board_id)
);

alter table public.hem_fpga_board_attempts
  add column if not exists implementation_workflow_id text;

alter table public.hem_fpga_board_attempts
  drop constraint if exists hem_fpga_board_attempts_deployment_architecture_check;
alter table public.hem_fpga_board_attempts
  add constraint hem_fpga_board_attempts_deployment_architecture_check check (
    deployment_architecture in ('fpga_onboard_cpu', 'fpga_soft_cpu', 'fpga_external_host')
  );

create index if not exists idx_hem_fpga_board_attempts_run
  on public.hem_fpga_board_attempts (hem_run_id, attempt_number);
create index if not exists idx_hem_fpga_board_attempts_user_created
  on public.hem_fpga_board_attempts (user_id, created_at desc);
create index if not exists idx_hem_fpga_board_attempts_status
  on public.hem_fpga_board_attempts (status, updated_at desc);

alter table public.hem_fpga_board_attempts enable row level security;

drop policy if exists hem_fpga_board_attempts_select_own on public.hem_fpga_board_attempts;
create policy hem_fpga_board_attempts_select_own
  on public.hem_fpga_board_attempts for select
  using (user_id = auth.uid()::text);

-- Publish the shared convergence contract on standalone FPGA workflows. The
-- backend dispatches these same fields through the product-convergence
-- orchestrator used by Physical AI/Application Intelligence.
update public.workflows w
set definitions = jsonb_set(
      coalesce(w.definitions, '{}'::jsonb),
      '{input_contract,fields}',
      coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'deployment_architecture'
         ) then jsonb_build_array(jsonb_build_object(
           'key','deployment_architecture','label','CPU / host placement','type','select','required',true,
           'defaultValue','fpga_external_host','options',jsonb_build_array('fpga_external_host','fpga_onboard_cpu','fpga_soft_cpu')
         )) else '[]'::jsonb end
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'automatic_board_convergence'
         ) then jsonb_build_array(jsonb_build_object(
           'key','automatic_board_convergence','label','Automatic board convergence','type','checkbox','required',false,'defaultValue',true
         )) else '[]'::jsonb end
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'max_fpga_board_convergence_attempts'
         ) then jsonb_build_array(jsonb_build_object(
           'key','max_fpga_board_convergence_attempts','label','Maximum board attempts','type','number','required',false,'defaultValue',3
         )) else '[]'::jsonb end
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'host_interface_plan'
         ) then jsonb_build_array(jsonb_build_object(
           'key','host_interface_plan','label','External host interface plan','type','json','required',false,
           'defaultValue',jsonb_build_object(
             'protocol','spi','role','fpga_peripheral','clock_mhz',10,'data_width_bits',8,
             'framing','register_command_response','flow_control','chip_select_and_status',
             'interrupt_signaling','optional_gpio','register_access','addressed_read_write'
           )
         )) else '[]'::jsonb end
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'soft_cpu_config'
         ) then jsonb_build_array(jsonb_build_object(
           'key','soft_cpu_config','label','Soft CPU configuration','type','json','required',false
         )) else '[]'::jsonb end
      || case when not exists (
           select 1 from jsonb_array_elements(coalesce(w.definitions#>'{input_contract,fields}', '[]'::jsonb)) f
           where f->>'key' = 'soft_cpu_integration_contract'
         ) then jsonb_build_array(jsonb_build_object(
           'key','soft_cpu_integration_contract','label','Soft CPU RTL and memory/interconnect contract','type','json','required',false
         )) else '[]'::jsonb end,
      true
    ),
    updated_at = now()
where w.user_id is null
  and w.name in ('FPGA_RTL_to_Bitstream', 'FPGA2RTL_to_Bitstream', 'FPGA_Implementation');
