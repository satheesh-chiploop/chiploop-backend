-- Align persisted plan definitions with the loop-based Stripe catalog.
-- Production-safe: updates existing plan rows only.

update public.subscription_plans
set
  price_monthly_usd = 19.99,
  price_monthly = 19.99,
  monthly_credit_limit = 500,
  monthly_credits = 500,
  discount_percent = 25,
  discount_months = 3,
  metadata = metadata || '{
    "stripe_price_key": "starter",
    "discount_percent": 25,
    "discount_months": 3,
    "included_loop_access": "1 Loop Core",
    "entitlements": {
      "plan_id": "starter",
      "plan_name": "Starter",
      "monthly_credits": 500,
      "max_api_keys": 0,
      "max_private_agents": 5,
      "sdk_cli_enabled": false,
      "agent_planner_enabled": true,
      "agent_factory_dry_run_enabled": true,
      "private_agent_save_enabled": true,
      "dag_optimization_enabled": true,
      "marketplace_submit_enabled": false,
      "agent_factory_write_enabled": false
    }
  }'::jsonb,
  updated_at = now()
where id = 'starter';

update public.subscription_plans
set
  price_monthly_usd = 49.99,
  price_monthly = 49.99,
  monthly_credit_limit = 2500,
  monthly_credits = 2500,
  discount_percent = 20,
  discount_months = 3,
  metadata = metadata || '{
    "stripe_price_key": "pro",
    "discount_percent": 20,
    "discount_months": 3,
    "included_loop_access": "3 Loop Core or 1 Loop Advanced",
    "most_popular": true,
    "entitlements": {
      "plan_id": "pro",
      "plan_name": "Pro",
      "monthly_credits": 2500,
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
  }'::jsonb,
  updated_at = now()
where id = 'pro';

update public.subscription_plans
set
  price_monthly_usd = 99.99,
  price_monthly = 99.99,
  monthly_credit_limit = 12000,
  monthly_credits = 12000,
  discount_percent = 20,
  discount_months = 3,
  metadata = metadata || '{
    "stripe_price_key": "pro_max",
    "discount_percent": 20,
    "discount_months": 3,
    "included_loop_access": "All Loops Advanced",
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
  }'::jsonb,
  updated_at = now()
where id = 'pro_max';
