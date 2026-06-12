-- Additive Studio workflow configuration contract.
-- The source of truth remains public.workflows.definitions so existing
-- workflow rows and Studio canvas restore behavior are not disrupted.

update public.workflows
set definitions = jsonb_set(
  coalesce(definitions, '{}'::jsonb),
  '{workflow_config_schema}',
  coalesce(definitions->'workflow_config_schema', '{"version":1,"fields":[]}'::jsonb),
  true
)
where status = 'saved'
  and coalesce(is_prebuilt, false) = false
  and not coalesce(definitions, '{}'::jsonb) ? 'workflow_config_schema';

update public.workflows
set definitions = jsonb_set(
  coalesce(definitions, '{}'::jsonb),
  '{default_run_config}',
  coalesce(definitions->'default_run_config', '{}'::jsonb),
  true
)
where status = 'saved'
  and coalesce(is_prebuilt, false) = false
  and not coalesce(definitions, '{}'::jsonb) ? 'default_run_config';
