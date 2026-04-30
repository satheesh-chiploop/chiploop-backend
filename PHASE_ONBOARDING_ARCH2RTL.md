# First-Run Arch2RTL Onboarding

## Purpose

New users now start with a guided Arch2RTL demo from `/apps` instead of being asked to choose from every app or Studio feature.

## User Flow

1. User logs in and lands on `/apps`.
2. Backend checks `/settings/onboarding`.
3. If onboarding is incomplete, `/apps` shows the guided Arch2RTL first-run experience.
4. User starts the demo.
5. The Arch2RTL app opens with a predefined PWM controller spec.
6. User runs the workflow, watches logs, downloads the ZIP, and is asked to inspect:
   - `rtl/*.sv`
   - `constraints/*.sdc`
   - `power/*.upf`
7. Download marks onboarding complete.
8. Returning visits to `/apps` show the normal Apps page.

## Backend Endpoints

- `GET /settings/onboarding`
- `POST /settings/onboarding`

Both require Supabase browser session auth.

Supported actions:

- `start`
- `complete`
- `skip`

## Supabase SQL

Run manually when ready:

```sql
supabase/migrations/phase_onboarding_arch2rtl.sql
```

This creates `public.user_onboarding` if it does not exist. It does not modify existing workflow, agent, billing, or artifact tables.

## Safety

- No frontend route changes.
- `/apps` remains the post-login destination.
- Existing Arch2RTL run endpoint is unchanged.
- Studio remains optional after onboarding.
- Onboarding state is user-scoped.
