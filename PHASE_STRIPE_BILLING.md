# Stripe Billing Foundation

This phase adds secure hosted billing for ChipLoop without storing card data.

## Model

- Stripe Checkout collects payment methods and starts the 7-day trial.
- Stripe Billing owns subscription charging, retries, Apple Pay/Google Pay/Link eligibility, and 3DS/SCA where available.
- Stripe Customer Portal lets users update cards and cancel/manage billing.
- ChipLoop stores only Stripe identifiers and subscription status in `user_subscriptions`.

## Required Stripe Setup

Create in Stripe:

- Products/prices for Starter, Pro, and Pro Max.
- A 25% coupon limited to the first 3 billing cycles if the intro discount is used.
- Customer Portal configuration.
- Webhook endpoint:
  - `https://<backend-host>/billing/stripe/webhook`

Recommended webhook events:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_succeeded`
- `invoice.payment_failed`

## Environment Variables

Backend only:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRICE_STARTER`
- `STRIPE_PRICE_PRO`
- `STRIPE_PRICE_PRO_MAX`
- `STRIPE_INTRO_COUPON_ID` optional
- `STRIPE_TRIAL_DAYS=7`
- `STRIPE_PAYMENT_GRACE_DAYS=3`
- `CHIPLOOP_APP_URL=https://<frontend-host>`

Never expose Stripe secret keys to the browser.

## SQL

Run after Phase 8D migrations:

```text
supabase/migrations/phase_stripe_billing_foundation.sql
```

This migration is additive. It adds Stripe id/status columns and indexes to `user_subscriptions`.

## Payment Failure Behavior

On `invoice.payment_failed`, ChipLoop marks the subscription `past_due` and sets a grace period. During grace, users can still fix billing. After grace expires, paid/high-cost actions are blocked until Stripe reports a successful payment.

## Manual Test Flow

1. Set Stripe test env vars.
2. Sign in to ChipLoop.
3. Open `/pricing`.
4. Click Start 7-day trial or a paid plan.
5. Complete Stripe Checkout with a test card.
6. Confirm `/settings/plan` shows Stripe-backed subscription state after webhook delivery.
7. Click Update payment to open Stripe Customer Portal.

## Notes

Cards and wallet payment methods are controlled by Stripe Checkout settings and account eligibility. Do not implement custom card forms until there is a clear reason.
