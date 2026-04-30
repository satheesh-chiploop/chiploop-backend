# Phase 8A Plans, Credits, and Entitlements

Phase 8A adds the backend foundation for plan-aware access control and credit accounting. It does not add Stripe, checkout, frontend pricing UI, or deployment automation.

## Plans

The backend defines five plans only:

| Plan | Price | Monthly credits | API keys | Private agents | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| Free | $0 | 100 | 1 test key | 1 | Limited Studio/SDK trial behavior |
| Starter | $29/mo | 1,000 | 1 | 5 | SDK/CLI and Studio dry-run enabled |
| Pro | $99/mo | 5,000 | 3 | 25 | Marketplace submit and DAG optimization enabled |
| Pro Max | $249/mo | 12,000 | 10 | 100 | Higher workflow limits flag enabled |
| Enterprise | Custom | Custom | Custom | Custom | Contact sales placeholder |

There is intentionally no Team plan.

## Credit Costs

Current action costs are conservative defaults:

- `app_workflow_run`: 50
- `sdk_workflow_run`: 50
- `studio_agent_planner`: 5
- `studio_agent_factory_dry_run`: 25
- `private_agent_save`: 10
- `dag_parallelism_analyze`: 10
- `marketplace_submit`: 5
- SDK list/status calls: 1

These values can be tuned after real usage data is available.

## Backend Modules

- `billing/models.py`: plan, entitlement, estimate, and ledger data models.
- `billing/entitlements.py`: static fallback plan definitions and credit cost table.
- `billing/repositories.py`: in-memory and Supabase repository boundaries.
- `billing/credit_service.py`: plan lookup, entitlement checks, credit estimates, and deduction.

If Supabase plan data is unavailable, users fall back to the Free plan.

## Enforcement Points

Phase 8A enforces entitlements and credits at existing backend boundaries:

- `/sdk/*`
- `/studio/agent-planner/plan`
- `/studio/agent-factory/dry-run`
- `/studio/user-agents` save
- `/studio/dag/*`
- `/settings/api-keys` create
- `/studio/user-agents/{id}/submit`

Existing App workflow endpoints are not changed in this phase.

## Settings Plan Endpoint

`GET /settings/plan` returns:

- current plan
- price
- monthly credits
- credits used
- credits remaining
- entitlements
- billing status placeholder

The endpoint uses browser Supabase session auth like the other `/settings/*` endpoints.

## SQL Migration

Review and run manually:

```bash
supabase/migrations/phase8a_plans_credits.sql
```

The migration:

- patches existing Phase 6 plan/credit tables
- seeds Free, Starter, Pro, Pro Max, and Enterprise
- adds safe indexes
- avoids drops, renames, and destructive changes

## Validation

Run:

```bash
pytest tests/test_phase8a_billing.py
pytest tests/test_api_keys_usage.py tests/test_phase7_browser_endpoints.py tests/test_private_user_agents.py
```

## Limitations

- No Stripe checkout or webhook handling yet.
- No frontend pricing or plan management UI yet.
- Credit costs are fixed constants for now.
- Enterprise custom limits are represented as entitlement metadata, not a sales workflow.
- App workflow run credit deduction is defined but not enforced yet to avoid changing existing App behavior.

## Next Steps

1. Add frontend Settings plan/usage display when ready.
2. Add Stripe products, checkout, and webhooks in a separate phase.
3. Add admin tools for subscription overrides and Enterprise limits.
4. Calibrate credit costs from production usage data.
