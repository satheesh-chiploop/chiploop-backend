# Webinar Sessions and Zoom Invites

## Current Session Schedule

The registration page shows exact Saturday sessions for the first four weeks:

- May 23, 2026, 9:00 AM PST
- May 23, 2026, 9:00 PM PST
- May 30, 2026, 9:00 AM PST
- May 30, 2026, 9:00 PM PST
- June 6, 2026, 9:00 AM PST
- June 6, 2026, 9:00 PM PST
- June 13, 2026, 9:00 AM PST
- June 13, 2026, 9:00 PM PST

Each slot is capped at 10 registered attendees. Full sessions are disabled in the dropdown.

## Backend Behavior

- `GET /webinar/sessions` returns sessions, capacity, booked seats, remaining seats, and full status.
- `POST /webinar/register` rejects full sessions with `preferred_session_full`.
- Capacity is enforced before saving the registration.

## Supabase SQL

Run this additive migration manually:

```sql
supabase/migrations/phase_webinar_sessions_capacity.sql
```

No new table is required because registrations are already stored in `webinar_registrations`.

## Zoom Invite Automation Strategy

Recommended production setup:

1. Create one Zoom meeting or webinar per session.
2. Store a mapping from `preferred_session` to Zoom meeting details.
3. After successful registration, send a transactional email with:
   - session date/time
   - Zoom join URL
   - calendar attachment
   - cancellation/update instructions

Lowest-cost practical option:

- Use one recurring Zoom meeting link for all sessions.
- Send the selected session time plus the same Zoom link by email.
- Use Resend, SendGrid, or another transactional email provider.

More robust option:

- Use Zoom Meeting/Webinar Registrants API.
- Backend creates a Zoom registrant after ChipLoop registration.
- Zoom returns a personalized join URL.
- Backend emails that personalized URL.

Do not expose Zoom API keys in frontend. Store Zoom and email provider secrets only in backend environment variables.

## Not Implemented Yet

This change does not send Zoom emails automatically yet. It prepares the session/capacity foundation. Zoom/email automation should be implemented after choosing the email provider and Zoom meeting model.
