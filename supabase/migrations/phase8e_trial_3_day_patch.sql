-- Phase 8E patch: change new-trial defaults from 7 days to 3 days.
-- Production-safe: updates plan metadata only. It does not shorten existing
-- user_subscriptions.trial_end_at values for active users.
-- Apply with STRIPE_TRIAL_DAYS=3 in the backend environment for new Checkout sessions.

update public.subscription_plans
set
  display_name = '3-day Trial',
  metadata = coalesce(metadata, '{}'::jsonb)
    || '{
      "requires_credit_card": true,
      "trial_days": 3,
      "converts_to": "starter",
      "trial_message": "Start free trial. $19.99/month after 3 days. Cancel anytime before trial ends."
    }'::jsonb
where id in ('trial', 'free');

comment on column public.user_subscriptions.trial_start_at is
  'Start timestamp for the Stripe-backed trial. Current default trial length is 3 days for new trials.';

comment on column public.user_subscriptions.trial_end_at is
  'End timestamp for the trial. Trial converts to Starter when auto_renew is true. Existing active trial end dates are preserved.';
