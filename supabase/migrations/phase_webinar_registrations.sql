-- Phase: Public webinar registration capture.
-- Run manually in Supabase. This migration is additive only.

create table if not exists public.webinar_registrations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null,
  company text,
  role text,
  loop_interest text,
  preferred_session text not null,
  questions text,
  source text not null default 'landing_page',
  status text not null default 'registered',
  created_at timestamptz not null default now()
);

create index if not exists idx_webinar_registrations_email
  on public.webinar_registrations (email);

create index if not exists idx_webinar_registrations_created_at
  on public.webinar_registrations (created_at desc);

create index if not exists idx_webinar_registrations_session
  on public.webinar_registrations (preferred_session);

comment on table public.webinar_registrations is
  'Public landing-page webinar registrations for ChipLoop Saturday demos.';

comment on column public.webinar_registrations.preferred_session is
  'Preferred Saturday webinar session: 9am_pst, 9pm_pst, or either.';
