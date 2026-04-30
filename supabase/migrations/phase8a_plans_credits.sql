-- Phase 8A: plan, credit, and entitlement foundation.
-- Production-safe: adds missing columns, indexes, and seed rows only.
-- Do not execute automatically; run manually in Supabase after review.

alter table if exists public.subscription_plans
  add column if not exists display_name text,
  add column if not exists price_monthly_usd integer,
  add column if not exists monthly_credits integer,
  add column if not exists active boolean not null default true;

alter table if exists public.user_subscriptions
  add column if not exists billing_status text not null default 'placeholder';

alter table if exists public.credit_ledger
  add column if not exists reference_id text;

create index if not exists idx_subscription_plans_active
  on public.subscription_plans (active);

create index if not exists idx_user_subscriptions_plan_status
  on public.user_subscriptions (plan_id, status);

create index if not exists idx_credit_ledger_user_event_created
  on public.credit_ledger (user_id, event_type, created_at desc);

comment on column public.subscription_plans.metadata is
  'Plan metadata includes Phase 8A entitlement flags. Stripe/product IDs can be added later without changing runtime code.';

comment on column public.user_subscriptions.billing_status is
  'Placeholder billing status for Phase 8A. Stripe is intentionally not integrated yet.';

comment on column public.credit_ledger.reference_id is
  'Optional workflow, generated agent, API key, or marketplace reference associated with a credit event.';

insert into public.subscription_plans (
  id,
  name,
  display_name,
  price_monthly_usd,
  monthly_credit_limit,
  monthly_credits,
  sdk_cli_enabled,
  agent_factory_write_enabled,
  active,
  metadata
) values
  (
    'free',
    'Free',
    'Free',
    0,
    100,
    100,
    true,
    false,
    true,
    '{
      "entitlements": {
        "plan_id": "free",
        "plan_name": "Free",
        "monthly_credits": 100,
        "max_api_keys": 1,
        "max_private_agents": 1,
        "sdk_cli_enabled": true,
        "agent_planner_enabled": true,
        "agent_factory_dry_run_enabled": true,
        "private_agent_save_enabled": true,
        "dag_optimization_enabled": false,
        "marketplace_submit_enabled": false,
        "agent_factory_write_enabled": false
      }
    }'::jsonb
  ),
  (
    'starter',
    'Starter',
    'Starter',
    29,
    1000,
    1000,
    true,
    false,
    true,
    '{
      "entitlements": {
        "plan_id": "starter",
        "plan_name": "Starter",
        "monthly_credits": 1000,
        "max_api_keys": 1,
        "max_private_agents": 5,
        "sdk_cli_enabled": true,
        "agent_planner_enabled": true,
        "agent_factory_dry_run_enabled": true,
        "private_agent_save_enabled": true,
        "dag_optimization_enabled": true,
        "marketplace_submit_enabled": false,
        "agent_factory_write_enabled": false
      }
    }'::jsonb
  ),
  (
    'pro',
    'Pro',
    'Pro',
    99,
    5000,
    5000,
    true,
    false,
    true,
    '{
      "entitlements": {
        "plan_id": "pro",
        "plan_name": "Pro",
        "monthly_credits": 5000,
        "max_api_keys": 3,
        "max_private_agents": 25,
        "sdk_cli_enabled": true,
        "agent_planner_enabled": true,
        "agent_factory_dry_run_enabled": true,
        "private_agent_save_enabled": true,
        "dag_optimization_enabled": true,
        "marketplace_submit_enabled": true,
        "agent_factory_write_enabled": false
      }
    }'::jsonb
  ),
  (
    'pro_max',
    'Pro Max',
    'Pro Max',
    249,
    12000,
    12000,
    true,
    false,
    true,
    '{
      "entitlements": {
        "plan_id": "pro_max",
        "plan_name": "Pro Max",
        "monthly_credits": 12000,
        "max_api_keys": 10,
        "max_private_agents": 100,
        "sdk_cli_enabled": true,
        "agent_planner_enabled": true,
        "agent_factory_dry_run_enabled": true,
        "private_agent_save_enabled": true,
        "dag_optimization_enabled": true,
        "marketplace_submit_enabled": true,
        "agent_factory_write_enabled": false,
        "higher_workflow_limits": true
      }
    }'::jsonb
  ),
  (
    'enterprise',
    'Enterprise',
    'Enterprise',
    null,
    1000000,
    null,
    true,
    false,
    true,
    '{
      "price_label": "custom",
      "contact_sales": true,
      "entitlements": {
        "plan_id": "enterprise",
        "plan_name": "Enterprise",
        "monthly_credits": null,
        "max_api_keys": 100,
        "max_private_agents": 1000,
        "sdk_cli_enabled": true,
        "agent_planner_enabled": true,
        "agent_factory_dry_run_enabled": true,
        "private_agent_save_enabled": true,
        "dag_optimization_enabled": true,
        "marketplace_submit_enabled": true,
        "agent_factory_write_enabled": false,
        "higher_workflow_limits": true,
        "custom_limits": true
      }
    }'::jsonb
  )
on conflict (id) do update set
  name = excluded.name,
  display_name = excluded.display_name,
  price_monthly_usd = excluded.price_monthly_usd,
  monthly_credit_limit = excluded.monthly_credit_limit,
  monthly_credits = excluded.monthly_credits,
  sdk_cli_enabled = excluded.sdk_cli_enabled,
  agent_factory_write_enabled = excluded.agent_factory_write_enabled,
  active = excluded.active,
  metadata = public.subscription_plans.metadata || excluded.metadata,
  updated_at = now();
