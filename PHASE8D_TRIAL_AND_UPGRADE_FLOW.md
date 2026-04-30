# Phase 8D Pricing and Upgrade Flow

Phase 8D defines the trial-to-paid ladder and backend-driven upgrade nudges. It does not add Stripe secrets or deploy checkout.

## Plans

| Plan | Price | Intro discount | Credits |
| --- | ---: | ---: | ---: |
| Trial | Free for 30 days | N/A | 100 trial credits |
| Starter | $19.99/month | $14.99 for first 3 cycles | 2,000/month |
| Pro | $39.99/month | $29.99 for first 3 cycles | 5,000/month |
| Pro Max | $59.99/month | $44.99 for first 3 cycles | 12,000/month |
| Enterprise | Custom | Custom | Custom |

Pro is marked as Most Popular in the frontend pricing page.

## Trial Rules

- Credit card is required at signup through Stripe checkout.
- Trial duration is 30 days.
- No charge during trial.
- Trial auto-converts to Starter unless canceled.
- Users can cancel before trial end.

## Backend

`user_subscriptions` is extended by `supabase/migrations/phase8d_trial_upgrade.sql`:

- `trial_start_at`
- `trial_end_at`
- `trial_status`
- `auto_renew`
- `discount_end_at`
- `plan_price_effective`
- `billing_cycle_count`

`subscription_plans` gains:

- `price_monthly`
- `discount_percent`
- `discount_months`

The billing service now calculates:

- trial active/expired state
- trial days remaining
- auto-conversion to Starter
- discounted price for the first 3 billing cycles
- remaining discount cycles
- upgrade suggestions for trial expiry, low credits, and feature limits

## API

`GET /settings/plan` returns the compatibility fields plus:

- `plan_name`
- `base_price`
- `discounted_price`
- `price_monthly`
- `credits`
- `trial_days_remaining`
- `discount_months_remaining`
- `suggested_upgrade`

`GET /settings/upgrade-status` returns the backend-driven upgrade recommendation.

Studio, SDK, and workflow responses include an `upgrade` / `upgrade_hint` object where practical.

## Stripe Configuration

Create Stripe prices for:

- Starter
- Pro
- Pro Max

Use:

- `trial_period_days = 30`
- a 25% coupon or promotion code limited to 3 billing cycles

Store Stripe price IDs in plan metadata or environment-backed server config. Do not expose Stripe secrets to the browser.

## Frontend

- `/pricing` shows the updated prices, intro discounts, and trial messaging.
- `/settings/plan` shows current plan, effective price, trial countdown, discount cycle remaining, and credits.
- `UpgradeNudge` uses `/api/settings/upgrade-status`, stores a local cooldown timestamp, and shows a simple upgrade modal when the user asks to review the prompt.

## Validation

Backend:

```bash
pytest tests
```

Frontend:

```bash
npx eslint app/pricing/page.tsx app/login/page.tsx app/settings/SettingsNav.tsx app/settings/plan/page.tsx components/UpgradeNudge.tsx
npm run build
```

## Risks

- Real Stripe checkout/webhooks are not implemented in this phase.
- Auto-conversion updates subscription state but does not charge until Stripe integration is added.
- Existing `free` plan rows are compatibility aliases for Trial and should be migrated deliberately.
