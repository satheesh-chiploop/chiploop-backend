# Phase 7H + 7I Private Agents And Marketplace Foundation

Date: 2026-04-28

## Product Model

ChipLoop now has a backend foundation for user-scoped private agents:

- Platform/global agents remain visible to all users.
- User-created agents are private to the creator by default.
- Private agents must not affect other users.
- Marketplace/global visibility requires explicit submission and later admin approval.
- System Planner should eventually use `platform/global agents + current user's private agents`.

This phase does not change frontend behavior and does not replace the legacy System Planner flow yet.

## SQL Migration

Generated migration:

```text
supabase/migrations/phase7h_private_agents_patch.sql
```

Run it manually in Supabase after review. It only uses `alter table ... add column if not exists`, comments, and safe indexes. It does not drop, rename, or destructively modify data.

The migration extends `public.agents` with:

- `agent_spec jsonb`
- `skills jsonb`
- `tools jsonb`
- `hooks jsonb`
- `generated_files jsonb`
- `registry_patch jsonb`
- `status text default 'private'`
- `visibility text default 'private'`
- `source text default 'studio_factory'`
- `submitted_at timestamptz`
- `reviewed_at timestamptz`
- `reviewed_by uuid`
- `review_notes text`

It also ensures `owner_id uuid` exists and adds indexes for:

- `owner_id`
- `visibility`
- `status`
- `loop_type`
- `(owner_id, visibility, status)`

## Private Agent Lifecycle

Private agent save:

1. Browser calls `POST /studio/user-agents`.
2. Backend requires Supabase session auth.
3. Backend writes to existing `public.agents`.
4. Backend stores:
   - `owner_id = authenticated user`
   - `is_custom = true`
   - `is_prebuilt = false`
   - `visibility = private`
   - `status = private`
   - Studio Contract metadata from Agent Factory dry-run where available.

Private agent list:

- `GET /studio/user-agents`
- Returns only the authenticated user's private custom agents.

Private agent delete:

- `DELETE /studio/user-agents/{id}`
- Enforces ownership through `owner_id`.

Private agent submit:

- `POST /studio/user-agents/{id}/submit`
- Sets `status = submitted`.
- Keeps `visibility = private`.
- Does not make the agent global.

## Marketplace Submission Lifecycle

Submission foundation reuses `marketplace_submissions` when available.

On submit:

- Backend verifies ownership.
- Backend updates the private agent to `status = submitted`.
- Backend attempts to insert a pending `marketplace_submissions` row:
  - `agent_id`
  - `submitted_by`
  - `agent_json`
  - `workflow_json = null`
  - `status = pending`

If `marketplace_submissions` is missing or has incompatible schema, the submit endpoint still records submitted status on the agent and returns the submission error for operational visibility.

Not implemented in this phase:

- Admin approval UI.
- Auto-approval.
- Global promotion.
- Billing or revenue share.

## Backend Modules Added

```text
user_agents/
  __init__.py
  models.py
  repository.py
  service.py
```

Supported service operations:

- `list_my_agents(user_id)`
- `save_private_agent(user_id, payload)`
- `delete_my_agent(user_id, agent_id)`
- `submit_my_agent(user_id, agent_id)`
- `effective_agent_catalog(user_id)`

## Browser-Safe Endpoints

Added to `browser_routes.py`:

- `GET /studio/user-agents`
- `POST /studio/user-agents`
- `DELETE /studio/user-agents/{id}`
- `POST /studio/user-agents/{id}/submit`

All require Supabase session auth through `require_browser_user`.

## System Planner Integration Plan

This phase adds the backend helper foundation but does not change the current Select Agents UI path.

Future System Planner lookup should call:

```python
effective_agents = platform/global agents + current_user_private_agents
```

The helper:

- Starts with `AGENT_CAPABILITIES`.
- Adds global/marketplace/approved agents from `public.agents`.
- Adds private agents where `owner_id = current user`.
- Excludes private agents owned by other users.
- Falls back to platform agents only for anonymous users.

## Legacy Generate Missing Agents

`/generate_missing_agents_batch` remains available for compatibility.

It is now explicitly treated as legacy because it:

- Writes Python files into backend agent directories.
- Does not produce full Studio Contract metadata.
- Is not review-only.

New browser flows should eventually use:

1. Agent Planner.
2. Agent Factory dry-run.
3. Generated Agent Review.
4. Save as private user agent.

## Next Frontend Steps

1. Update Generate Missing Agents in System Planner to resolve missing agents sequentially.
2. For each missing agent, call Agent Planner.
3. If creation is needed, call Agent Factory dry-run.
4. Let the user review the generated AgentSpec, files, registry patch, and risks.
5. Call `POST /studio/user-agents` to save reviewed output as private.
6. Refresh System Planner agent catalog.
7. Enable Build Workflow after missing agents are resolved.
8. Add Submit to Marketplace action later.

## Risks

- New private-agent endpoints require the Phase 7H SQL migration before production use.
- Existing legacy custom-agent endpoints still have historical behavior and should be retired gradually.
- Marketplace submission insert is best-effort until the exact table schema is confirmed.
- Effective catalog is a foundation helper and is not yet wired into the active System Planner selection path.
