-- Loop add-on entitlement storage for Stripe-backed loop subscriptions.
-- Production-safe: additive table/indexes only. Run manually in Supabase before enabling self-serve loop add-ons.

create table if not exists public.user_loop_entitlements (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  loop_key text not null,
  loop_level text not null default 'core',
  addon_type text,
  status text not null default 'active',
  stripe_customer_id text,
  stripe_subscription_id text,
  stripe_checkout_session_id text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id, loop_key)
);

create index if not exists idx_user_loop_entitlements_user_status
  on public.user_loop_entitlements(user_id, status);

create index if not exists idx_user_loop_entitlements_stripe_subscription
  on public.user_loop_entitlements(stripe_subscription_id)
  where stripe_subscription_id is not null and stripe_subscription_id <> '';

comment on table public.user_loop_entitlements is
  'ChipLoop loop access granted by base plans or Stripe loop add-on subscriptions. Supabase remains the source of truth for access.';

comment on column public.user_loop_entitlements.loop_key is
  'Subscription loop key such as digital_design, digital_implementation, mixed_signal, firmware_software, or validation.';

comment on column public.user_loop_entitlements.loop_level is
  'core or advanced.';
