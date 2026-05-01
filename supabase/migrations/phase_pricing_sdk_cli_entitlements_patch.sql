-- Phase pricing patch: SDK/CLI is a Pro, Pro Max, and Enterprise feature.
-- Run manually in Supabase. This migration is additive/non-destructive.

update public.subscription_plans
set entitlements =
  jsonb_set(
    jsonb_set(coalesce(entitlements, '{}'::jsonb), '{sdk_cli_enabled}', 'false'::jsonb, true),
    '{max_api_keys}',
    '0'::jsonb,
    true
  )
where id in ('trial', 'free', 'starter');

update public.subscription_plans
set entitlements =
  jsonb_set(coalesce(entitlements, '{}'::jsonb), '{sdk_cli_enabled}', 'true'::jsonb, true)
where id in ('pro', 'pro_max', 'enterprise');

comment on column public.subscription_plans.entitlements is
  'Plan capability flags. SDK/CLI developer automation is enabled only for Pro, Pro Max, and Enterprise.';
