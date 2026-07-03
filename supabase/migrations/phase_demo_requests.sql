-- Phase: Public demo request capture.
-- Run manually in Supabase. This migration is additive only.

create table if not exists public.demo_requests (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null,
  phone text,
  organization_type text not null,
  organization_name text,
  role text,
  interest_area text,
  preferred_time text,
  message text,
  source text not null default 'landing_page',
  status text not null default 'new',
  notification_to text not null default 'chiploop.agx@gmail.com',
  notification_status text not null default 'pending',
  notification_error text,
  notification_sent_at timestamptz,
  created_at timestamptz not null default now(),
  constraint demo_requests_organization_type_check
    check (organization_type in ('Individual', 'Company', 'University')),
  constraint demo_requests_status_check
    check (status in ('new', 'contacted', 'scheduled', 'closed')),
  constraint demo_requests_notification_status_check
    check (notification_status in ('pending', 'sent', 'failed', 'not_configured'))
);

create index if not exists idx_demo_requests_email
  on public.demo_requests (email);

create index if not exists idx_demo_requests_created_at
  on public.demo_requests (created_at desc);

create index if not exists idx_demo_requests_status
  on public.demo_requests (status);

comment on table public.demo_requests is
  'Public Book Demo requests from ChipLoop landing and marketing pages.';

comment on column public.demo_requests.notification_status is
  'Email notification result for the internal ChipLoop copy.';
