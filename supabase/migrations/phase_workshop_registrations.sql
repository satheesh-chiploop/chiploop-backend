-- Phase: Paid two-day workshop registration and Stripe Checkout tracking.
-- Additive only. Run manually in Supabase before enabling paid workshop checkout.

create table if not exists public.workshop_registrations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null,
  company text,
  role text,
  loop_interest text,
  batch_id text not null,
  questions text,
  source text not null default 'workshop_page',
  status text not null default 'pending_payment',
  stripe_checkout_session_id text,
  stripe_customer_id text,
  stripe_payment_intent_id text,
  paid_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_workshop_registrations_email
  on public.workshop_registrations (email);

create index if not exists idx_workshop_registrations_batch_status
  on public.workshop_registrations (batch_id, status);

create unique index if not exists idx_workshop_registrations_checkout_session
  on public.workshop_registrations (stripe_checkout_session_id)
  where stripe_checkout_session_id is not null and stripe_checkout_session_id <> '';

comment on table public.workshop_registrations is
  'Paid two-day ChipLoop workshop registrations. Payment completion is confirmed by Stripe checkout.session.completed webhooks.';

comment on column public.workshop_registrations.batch_id is
  'Workshop batch id, e.g. 2026-05-30_09-30am_pt.';
