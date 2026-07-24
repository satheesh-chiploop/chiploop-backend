-- Make workflow artifact discovery a real Supabase source-of-truth index.
-- Artifact bodies remain in Supabase Storage under backend/workflows/<workflow_id>/...
-- The workflows.artifacts column should be JSONB so dashboards can discover all
-- generated summaries without hitting the old VARCHAR-size compaction path.

do $$
declare
  col_type text;
begin
  create or replace function pg_temp.try_parse_jsonb(value text)
  returns jsonb
  language plpgsql
  as $fn$
  begin
    return value::jsonb;
  exception when others then
    return null;
  end;
  $fn$;

  select data_type
    into col_type
  from information_schema.columns
  where table_schema = 'public'
    and table_name = 'workflows'
    and column_name = 'artifacts';

  if col_type is null then
    alter table public.workflows
      add column artifacts jsonb not null default '{}'::jsonb;
  elsif col_type <> 'jsonb' then
    alter table public.workflows
      alter column artifacts drop default;

    alter table public.workflows
      alter column artifacts type jsonb
      using case
        when artifacts is null then '{}'::jsonb
        when trim(artifacts::text) = '' then '{}'::jsonb
        else coalesce(pg_temp.try_parse_jsonb(artifacts::text), jsonb_build_object('legacy', artifacts::text))
      end;

    alter table public.workflows
      alter column artifacts set default '{}'::jsonb;

    update public.workflows
      set artifacts = '{}'::jsonb
      where artifacts is null;

    alter table public.workflows
      alter column artifacts set not null;
  else
    alter table public.workflows
      alter column artifacts set default '{}'::jsonb;

    update public.workflows
      set artifacts = '{}'::jsonb
      where artifacts is null;
  end if;
end $$;

comment on column public.workflows.artifacts is
  'JSONB index of Supabase Storage artifact paths for a workflow. Artifact content lives in the artifacts bucket.';
