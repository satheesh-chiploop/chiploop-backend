# Phase 13 Developer Ecosystem

ChipLoop is now programmable through a small, stable SDK/CLI surface. The browser App and Studio continue to use their existing browser-safe endpoints; external automation should use `/sdk/v1/*`.

## Public API Surface

Stable v1 endpoints:

- `GET /sdk/v1/workflows`
- `POST /sdk/v1/workflows/run`
- `GET /sdk/v1/workflows/{workflow_id}/status`
- `GET /sdk/v1/workflows/{workflow_id}/artifacts`
- `GET /sdk/v1/workflows/{workflow_id}/artifacts/{artifact_path}`
- `GET /sdk/v1/agents`
- `GET /sdk/v1/usage`
- `GET /sdk/v1/plan`

Beta v1 endpoints:

- `POST /sdk/v1/studio/plan-agent`
- `POST /sdk/v1/studio/generate-agent`

Compatibility endpoints:

- Existing `/sdk/*` endpoints remain available for older clients.

Internal/browser endpoints:

- `/studio/*`
- `/settings/*`
- `/admin/*`
- Existing App workflow endpoints outside `/sdk/v1`

Browser endpoints are not public API. SDK/CLI integrations should use `/sdk/v1`.

## Authentication

Use an API key. SDK and CLI access are available on Pro, Pro Max, and Enterprise plans:

```bash
set CHIPLOOP_BASE_URL=https://your-backend.example
set CHIPLOOP_API_KEY=cl_live_...
```

The SDK and CLI send:

```text
Authorization: Bearer <api_key>
```

## Python Quickstart

```python
from chiploop_sdk import ChipLoopClient

client = ChipLoopClient()

run = client.run_workflow(
    "arch2rtl",
    spec_text="Create a simple PWM controller with configurable duty cycle."
)

print(run.workflow_id)
print(client.get_workflow_status(run.workflow_id).status)
print(client.list_artifacts(run.workflow_id))
```

## CLI Quickstart

```bash
chiploop doctor
chiploop workflows list
chiploop run arch2rtl --spec examples/sdk/arch2rtl_spec.md
chiploop status <workflow_id>
chiploop artifacts list <workflow_id>
chiploop artifacts download <workflow_id> --name rtl/top.v --out ./outputs
chiploop usage
chiploop plan
```

Use `--json` for automation:

```bash
chiploop workflows list --json
```

## Error Contract

SDK errors raise `ChipLoopAPIError` with:

- `status_code`
- `response_text`
- `code`
- `detail`
- `request_id` when provided by the backend

The CLI returns a nonzero exit code and prints errors to stderr.

## Stability Policy

Stable v1 endpoints avoid breaking changes. ChipLoop may add response fields, but stable fields should not be removed without a deprecation window.

Breaking changes should use a future `/sdk/v2` path.

Beta endpoints may change with notice:

- Studio Agent Planner
- Studio Agent Factory dry-run

## Near-Term Limitations

- SDK is synchronous only.
- No webhook API yet.
- No public plugin marketplace API yet.
- Browser `/studio/*` and `/settings/*` endpoints are not SDK API.
- API reference is intentionally markdown-first until the surface is proven by usage.

