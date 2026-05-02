-- Stripe Billing foundation for hosted Checkout, Customer Portal, and webhooks.
-- Production-safe: additive columns/indexes only. No destructive changes.
-- Do not execute automatically; review and run manually in Supabase.

alter table if exists public.user_subscriptions
  add column if not exists stripe_customer_id text,
  add column if not exists stripe_subscription_id text,
  add column if not exists stripe_price_id text,
  add column if not exists stripe_checkout_session_id text,
  add column if not exists cancel_at_period_end boolean not null default false,
  add column if not exists payment_failure_at timestamptz,
  add column if not exists grace_period_end_at timestamptz;

create unique index if not exists idx_user_subscriptions_stripe_subscription_id
  on public.user_subscriptions (stripe_subscription_id)
  where stripe_subscription_id is not null and stripe_subscription_id <> '';

create index if not exists idx_user_subscriptions_stripe_customer_id
  on public.user_subscriptions (stripe_customer_id)
  where stripe_customer_id is not null and stripe_customer_id <> '';

create index if not exists idx_user_subscriptions_billing_status_grace
  on public.user_subscriptions (billing_status, grace_period_end_at);

comment on column public.user_subscriptions.stripe_customer_id is
  'Stripe Customer id. Card/payment method data remains in Stripe and is never stored in ChipLoop.';

comment on column public.user_subscriptions.stripe_subscription_id is
  'Stripe Subscription id used for webhook idempotent subscription updates.';

comment on column public.user_subscriptions.stripe_price_id is
  'Stripe Price id associated with the current self-serve plan.';

comment on column public.user_subscriptions.stripe_checkout_session_id is
  'Last Stripe Checkout Session id that created or changed the subscription.';

comment on column public.user_subscriptions.cancel_at_period_end is
  'True when Stripe subscription is scheduled to cancel at period end.';

comment on column public.user_subscriptions.payment_failure_at is
  'Timestamp of most recent Stripe invoice.payment_failed webhook.';

comment on column public.user_subscriptions.grace_period_end_at is
  'After this timestamp, past_due/unpaid subscriptions should be blocked from paid/high-cost actions until payment is updated.';
