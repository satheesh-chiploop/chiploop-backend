# Phase 7A Backend Endpoints

## Purpose

Phase 7A adds browser-safe Studio and Settings endpoints for the frontend. These endpoints are separate from `/sdk/*` because `/sdk/*` is designed for CLI/SDK automation and requires ChipLoop API keys. Browser users already authenticate through Supabase sessions and should not paste SDK keys into the app.

Existing App workflow endpoints and `/sdk/*` endpoints remain unchanged.

## Authentication Model

- `/studio/*` and `/settings/*` require `Authorization: Bearer <supabase-session-token>`.
- The backend validates the Supabase session token and derives `user_id`.
- Settings endpoints enforce ownership by `user_id`.
- API keys are never returned raw except immediately after creation.
- Registry summary returns counts only and does not expose raw YAML.

## Endpoints

### `GET /studio/registry/summary`

Returns safe registry counts.

Example response:

```json
{
  "status": "ok",
  "agent_count": 151,
  "workflow_count": 6,
  "skill_count": 12,
  "tool_count": 11,
  "hook_count": 6,
  "command_count": 2
}
```

### `POST /studio/agent-planner/plan`

Calls the existing Agent Planner without modifying files or registries.

Example request:

```json
{
  "natural_language_request": "Create an STA constraints agent",
  "domain": "system",
  "loop_type": "rtl2gds",
  "inputs": ["clock definition"],
  "outputs": ["sdc constraints"]
}
```

Example response shape:

```json
{
  "status": "ok",
  "result": {
    "requested_capability": "Create an STA constraints agent",
    "exact_matches": [],
    "similar_matches": [],
    "reusable_skills": [],
    "reusable_tools": [],
    "reusable_hooks": [],
    "missing_capabilities": [],
    "recommendation": "create_new",
    "confidence_score": 0.42,
    "explanation": "..."
  }
}
```

### `POST /studio/agent-factory/dry-run`

Calls the existing Agent Factory in dry-run mode only. The endpoint ignores any requested write mode and always returns `written_files: []`.

Example request:

```json
{
  "name": "System STA Constraint Agent",
  "natural_language_request": "Generate SDC timing constraints from a clock/reset spec",
  "domain": "system",
  "loop_type": "rtl2gds"
}
```

### `POST /studio/dag/preview`

Builds a DAG preview and returns dry-run execution order and parallel groups. It never executes agents.

Accepted payloads:

- `{ "agents": ["Digital Spec Agent", "Digital RTL Agent"], "loop_type": "digital" }`
- `{ "dag": { "nodes": [...], "edges": [...] } }`
- `{ "graph": { "nodes": [...], "edges": [...] } }`

### `POST /studio/dag/validate`

Validates a DAG and returns errors/warnings.

Example response:

```json
{
  "status": "ok",
  "valid": true,
  "errors": [],
  "warnings": []
}
```

### `GET /settings/api-keys`

Returns API key metadata for the authenticated user only.

Fields:

- `id`
- `key_prefix`
- `name`
- `created_at`
- `last_used_at`
- `revoked_at`

Raw keys and hashes are not returned.

### `POST /settings/api-keys`

Creates an API key owned by the authenticated user. The raw key is returned once.

Example request:

```json
{
  "name": "local CLI",
  "environment": "test"
}
```

Example response:

```json
{
  "status": "ok",
  "api_key": "cl_test_...",
  "api_key_metadata": {
    "id": "...",
    "key_prefix": "cl_test_...",
    "name": "local CLI",
    "created_at": "...",
    "last_used_at": null,
    "revoked_at": null
  }
}
```

### `POST /settings/api-keys/{id}/revoke`

Revokes an API key owned by the authenticated user. Attempts to revoke another user's key return 404.

### `GET /settings/usage`

Returns a frontend-friendly usage summary scoped to the authenticated user.

## Security Rules

- Browser endpoints use Supabase session auth, not ChipLoop SDK API keys.
- `/sdk/*` continues to require ChipLoop API keys.
- Agent Factory browser access is dry-run only.
- DAG preview and validation never execute agents.
- Settings endpoints enforce user ownership.
- No backend secrets, service keys, raw registry YAML, or raw API key hashes are returned.

## Frontend Integration Notes

- Frontend can call these through Vercel rewrites as `/api/studio/...` and `/api/settings/...`.
- Include the Supabase session access token as `Authorization: Bearer <token>`.
- Keep Apps on existing workflow endpoints.
- Use Studio endpoints only for power-user panels.

## Known Limitations

- Usage is limited to existing API usage events; full billing/credit ledger UI is out of scope.
- Agent Factory promotion remains manual and is not exposed here.
- DAG execution remains opt-in backend infrastructure and is not exposed through these browser endpoints.

## Next Phase 7B Frontend Steps

1. Add protected Settings pages for API keys and usage.
2. Add Studio panels for Agent Planner and Agent Factory dry-run review.
3. Add DAG preview/validation panel.
4. Keep all existing App runners unchanged.
