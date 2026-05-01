-- Phase 8D: trial-to-paid conversion and upgrade nudge foundation.
-- Production-safe: additive columns, indexes, and plan seed updates only.
-- Do not execute automatically; review and run manually in Supabase.

alter table if exists public.user_subscriptions
  add column if not exists trial_start_at timestamptz,
  add column if not exists trial_end_at timestamptz,
  add column if not exists trial_status text not null default 'active',
  add column if not exists auto_renew boolean not null default true,
  add column if not exists discount_end_at timestamptz,
  add column if not exists plan_price_effective numeric,
  add column if not exists billing_cycle_count integer not null default 0;

alter table if exists public.subscription_plans
  add column if not exists price_monthly numeric,
  add column if not exists discount_percent integer not null default 0,
  add column if not exists discount_months integer not null default 0;

alter table if exists public.subscription_plans
  alter column price_monthly_usd type numeric using price_monthly_usd::numeric;

create index if not exists idx_user_subscriptions_trial_status_end
  on public.user_subscriptions (trial_status, trial_end_at);

create index if not exists idx_user_subscriptions_user_trial
  on public.user_subscriptions (user_id, trial_status, trial_end_at desc);

comment on column public.user_subscriptions.trial_start_at is
  'Start timestamp for the 7-day Stripe-backed trial.';

comment on column public.user_subscriptions.trial_end_at is
  'End timestamp for the 7-day trial. Trial converts to Starter when auto_renew is true.';

comment on column public.user_subscriptions.trial_status is
  'Trial lifecycle: active, expired, converted, canceled.';

comment on column public.user_subscriptions.auto_renew is
  'True means expired trials convert to Starter; false means cancel before conversion.';

comment on column public.user_subscriptions.discount_end_at is
  'Timestamp when introductory discount eligibility ends.';

comment on column public.user_subscriptions.plan_price_effective is
  'Current effective monthly price after active introductory discount.';

comment on column public.user_subscriptions.billing_cycle_count is
  'Completed paid billing cycles. Phase 8D discount applies while count is below 3.';

comment on column public.subscription_plans.price_monthly is
  'Canonical monthly plan price. price_monthly_usd remains for backward compatibility.';

comment on column public.subscription_plans.discount_percent is
  'Introductory discount percentage. Phase 8D uses 25 for paid self-serve plans.';

comment on column public.subscription_plans.discount_months is
  'Number of initial paid billing cycles with discount. Phase 8D uses 3.';

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
  price_monthly,
  discount_percent,
  discount_months,
  metadata
) values
  (
    'trial',
    'Trial',
    '7-day Trial',
    0,
    100,
    100,
    true,
    false,
    true,
    0,
    0,
    0,
    '{
      "requires_credit_card": true,
      "trial_days": 7,
      "converts_to": "starter",
      "trial_message": "Start free trial. $19.99/month after 7 days. Cancel anytime before trial ends.",
      "entitlements": {
        "plan_id": "trial",
        "plan_name": "Trial",
        "monthly_credits": 100,
        "max_api_keys": 0,
        "max_private_agents": 1,
        "sdk_cli_enabled": false,
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
    19.99,
    2000,
    2000,
    true,
    false,
    true,
    19.99,
    25,
    3,
    '{
      "stripe_price_key": "starter",
      "discount_percent": 25,
      "discount_months": 3,
      "entitlements": {
        "plan_id": "starter",
        "plan_name": "Starter",
        "monthly_credits": 2000,
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
    39.99,
    5000,
    5000,
    true,
    false,
    true,
    39.99,
    25,
    3,
    '{
      "stripe_price_key": "pro",
      "discount_percent": 25,
      "discount_months": 3,
      "most_popular": true,
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
    59.99,
    12000,
    12000,
    true,
    false,
    true,
    59.99,
    25,
    3,
    '{
      "stripe_price_key": "pro_max",
      "discount_percent": 25,
      "discount_months": 3,
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
  )
on conflict (id) do update set
  name = excluded.name,
  display_name = excluded.display_name,
  price_monthly_usd = excluded.price_monthly_usd,
  monthly_credit_limit = excluded.monthly_credit_limit,
  monthly_credits = excluded.monthly_credits,
  price_monthly = excluded.price_monthly,
  discount_percent = excluded.discount_percent,
  discount_months = excluded.discount_months,
  sdk_cli_enabled = excluded.sdk_cli_enabled,
  agent_factory_write_enabled = excluded.agent_factory_write_enabled,
  active = excluded.active,
  metadata = public.subscription_plans.metadata || excluded.metadata,
  updated_at = now();

-- Keep old Free rows inactive for compatibility only. New signup should use Trial.
update public.subscription_plans
set active = false,
    metadata = metadata || '{"deprecated": true, "replacement_plan_id": "trial"}'::jsonb,
    updated_at = now()
where id = 'free';
