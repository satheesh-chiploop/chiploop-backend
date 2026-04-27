# Phase 4A-lite: SDK API Keys and Usage Tracking

Phase 4A-lite adds minimum backend API-key enforcement for SDK/CLI endpoints only.

No frontend behavior, App workflow behavior, existing agents, deployment settings, Stripe integration, or pricing UI changed.

## Protected Scope

Only `/sdk/*` endpoints require a ChipLoop API key:

- `GET /sdk/agents`
- `GET /sdk/workflows`
- `GET /sdk/workflows/{workflow_id}/status`
- `POST /sdk/studio/plan-agent`
- `POST /sdk/studio/generate-agent`

Existing App endpoints such as `/run_workflow`, `/list_agents`, `/list_artifacts/{workflow_id}`, and `/download_artifacts/...` are unchanged for now.

## API Key Format

Supported key prefixes:

- `cl_test_<random>`
- `cl_live_<random>`

Raw API keys are never stored. Persistence stores:

- `key_hash`
- `key_prefix`
- `user_id`
- `name`
- `created_at`
- `revoked_at`
- `last_used_at`

## Storage

The service supports:

- Supabase-backed storage through tables named `api_keys` and `api_usage_events`.
- Local JSON storage for development when `CHIPLOOP_LOCAL_API_KEY_STORE` is set.
- In-memory storage for tests.

No secrets are hardcoded.

## Local Test Key Flow

Create a local test key:

```powershell
python -m auth_api_keys.create_test_key --user-id dev-user --name local-test --store .chiploop/local_api_keys.json
```

The command prints the raw key once. Start the backend with the same local store path:

```powershell
$env:CHIPLOOP_LOCAL_API_KEY_STORE='.chiploop/local_api_keys.json'
```

Then use the key from SDK/CLI:

```powershell
$env:CHIPLOOP_BASE_URL='http://localhost:8000'
$env:CHIPLOOP_API_KEY='cl_test_...'
chiploop agents list
```

## SDK/CLI Auth

The Python SDK already sends:

```text
Authorization: Bearer <api_key>
```

when `api_key` is passed to `ChipLoopClient` or `CHIPLOOP_API_KEY` is set.

## Usage Events

Each protected SDK endpoint records a minimal usage event:

- `user_id`
- `api_key_id`
- `endpoint`
- `workflow_id` when available
- `event_type`
- `created_at`

Current event types:

- `sdk_agents_list`
- `sdk_workflows_list`
- `sdk_workflow_status`
- `sdk_studio_plan_agent`
- `sdk_studio_generate_agent`

## Entitlements

The entitlement stub currently returns:

- `sdk_cli_enabled = true`
- `monthly_credit_limit = 1000000`
- `agent_factory_write_enabled = false`

Studio Factory write mode through `/sdk/studio/generate-agent` is blocked by default unless `CHIPLOOP_AGENT_FACTORY_WRITE_ENABLED=true` is explicitly set in the backend environment.

## Tests

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_api_keys_usage.py
```

Full current suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py tests/test_studio_contract_registry.py tests/test_studio_planner_factory.py tests/test_chiploop_sdk_cli.py tests/test_api_keys_usage.py
```

## Current Limitations

- No Stripe integration.
- No pricing UI.
- No frontend API-key management UI.
- No monthly credit decrement logic yet.
- App endpoints are intentionally not API-key protected in this phase.

## Next Steps

- Add Supabase migrations for `api_keys` and `api_usage_events`.
- Add frontend API-key management UI.
- Add plan and quota enforcement backed by persisted subscription state.
- Decide whether artifact endpoints should move under `/sdk/*` wrappers before protecting downloads.
