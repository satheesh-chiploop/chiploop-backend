# Agent Marketplace Ecosystem

## Scope

This phase adds the first production-safe marketplace foundation:

- Admin approval endpoints and UI
- Marketplace listing/detail pages
- Agent search/filter
- Install marketplace agent into a user workspace
- Ratings/reviews

Private user agents remain private. Approval creates a separate marketplace listing and immutable version record.

## Backend

New browser session-auth endpoints:

- `GET /marketplace/agents`
- `GET /marketplace/agents/{slug}`
- `POST /marketplace/agents/{id}/install`
- `GET /marketplace/agents/{id}/reviews`
- `POST /marketplace/agents/{id}/reviews`

New admin endpoints:

- `GET /admin/marketplace/submissions`
- `GET /admin/marketplace/submissions/{id}`
- `POST /admin/marketplace/submissions/{id}/approve`
- `POST /admin/marketplace/submissions/{id}/reject`
- `POST /admin/marketplace/submissions/{id}/request-changes`

Admin access is allowed when the Supabase JWT has role `admin`, `platform_admin`, or `marketplace_admin`, or when `CHIPLOOP_ADMIN_USER_IDS` contains the user id.

## SQL

Run manually:

```sql
supabase/migrations/phase_marketplace_ecosystem.sql
```

Creates:

- `marketplace_agent_listings`
- `marketplace_agent_versions`
- `marketplace_installs`
- `marketplace_reviews`

Also patches `marketplace_submissions` with review metadata columns if missing.

## Frontend

Routes added:

- `/marketplace`
- `/marketplace/agents/[slug]`
- `/admin/marketplace`

Marketplace installs create a private user-scoped agent through the existing private-agent path, so installed agents appear in Studio under My Agents.

## Safety

- No global registry mutation from install.
- No private agent becomes public automatically.
- Approval creates a separate listing.
- Versions are recorded separately.
- Reviews are one-per-user per listing by database constraint.
- Marketplace/admin pages require login.

## Next Enterprise Hardening

- Organization-scoped marketplace
- Reviewer audit logs
- Validation scan results before approval
- Signed package/version checksums
- Explicit version upgrade flow for installed agents
- Admin review RBAC backed by Supabase custom claims
