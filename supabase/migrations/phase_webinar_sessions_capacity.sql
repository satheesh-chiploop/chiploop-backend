-- Phase: Webinar session capacity support.
-- Run manually in Supabase. This migration is additive only.

-- Registration capacity is enforced by backend code using preferred_session counts.
-- This index keeps per-session counts fast as registrations grow.
create index if not exists idx_webinar_registrations_session_status
  on public.webinar_registrations (preferred_session, status);

comment on column public.webinar_registrations.preferred_session is
  'Exact webinar session id, e.g. 2026-05-23_09am_pt. Backend caps registered rows at 10 per session.';
