-- Physical AI workflow creation writes both public.workflows and public.runs.
-- Keep their loop_type contracts aligned so Supabase remains authoritative.
alter table if exists public.runs
  drop constraint if exists runs_loop_type_chk;

alter table if exists public.runs
  drop constraint if exists runs_loop_type_check;

alter table if exists public.runs
  add constraint runs_loop_type_chk
  check (
    loop_type is null
    or loop_type in ('digital', 'analog', 'system', 'embedded', 'validation', 'fpga', 'physical_ai')
  );
