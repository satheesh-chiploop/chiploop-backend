# Phase 4: ChipLoop Python SDK and CLI v1

Phase 4 adds developer and automation access to ChipLoop without changing the browser App, existing workflow execution, agents, Studio Planner, or Studio Factory behavior.

## Product Separation

- App: browser workflow execution for simple user flows.
- Studio: registry-based planning and safe generated-agent review artifacts.
- SDK/CLI: developer and automation access to existing backend APIs.
- IDE plugin: later phase, not included here.

## Installation

From the backend repo:

```powershell
pip install -e .
```

This installs the `chiploop` console command from `pyproject.toml`.

The editable package intentionally includes only `chiploop_sdk` and `auth_api_keys`.
Backend runtime packages such as `agents`, `registry`, `services`, generated artifacts, and tests are excluded from SDK/CLI packaging.

Local module usage without installation:

```powershell
python -m chiploop_sdk.cli --base-url http://localhost:8000 doctor
```

## Environment Variables

- `CHIPLOOP_BASE_URL`: backend URL, for example `http://localhost:8000`.
- `CHIPLOOP_API_KEY`: optional bearer token/JWT.

The SDK never hardcodes secrets. If no base URL is provided, it raises a clear configuration error.

## Python SDK

```python
from chiploop_sdk import ChipLoopClient

client = ChipLoopClient(base_url="http://localhost:8000")

run = client.run_workflow(
    "digital",
    spec_text="Create a counter with enable and reset.",
    inputs={"loop_type": "digital"},
)

status = client.get_workflow_status(run.workflow_id)
artifacts = client.list_artifacts(run.workflow_id)
```

Studio Planner:

```python
plan = client.plan_agent({
    "natural_language_request": "Generate RTL compile repair feedback",
    "domain": "digital",
    "loop_type": "digital",
})
```

Studio Factory dry-run:

```python
result = client.generate_agent({
    "name": "System STA Constraint Agent",
    "natural_language_request": "Create system-level STA constraints.",
    "domain": "physical_design",
    "loop_type": "system",
}, dry_run=True)
```

## CLI Examples

```powershell
chiploop doctor
chiploop workflows list
chiploop agents list
chiploop run digital --spec .\spec.txt
chiploop run digital --spec-text "Create a small UART register block"
chiploop status <workflow_id>
chiploop artifacts list <workflow_id>
chiploop artifacts download <workflow_id> --name digital/rtl/top.sv --out .\downloads
chiploop studio plan-agent --request examples/studio_planner/request.json
chiploop studio generate-agent --request examples/studio_factory/system_sta_constraint_agent_request.json --dry-run
```

JSON output:

```powershell
chiploop --json workflows list
```

## Backend Compatibility Wrappers

The SDK keeps using existing endpoints where stable:

- `POST /run_workflow`
- `GET /list_artifacts/{workflow_id}`
- `GET /download_artifacts/{workflow_id}/{rel_path}`

Thin SDK wrappers were added for stable metadata/status access:

- `GET /sdk/workflows`
- `GET /sdk/workflows/{workflow_id}/status`
- `GET /sdk/agents`
- `POST /sdk/studio/plan-agent`
- `POST /sdk/studio/generate-agent`

These wrappers do not replace or change existing frontend endpoints.

## Safety

- Artifact downloads reject absolute paths and `..` traversal.
- CLI writes downloaded artifacts only under the requested output directory.
- Studio generate-agent defaults to dry-run.
- CLI prints a warning when write mode is requested for Studio generation.

## Tests

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_chiploop_sdk_cli.py
```

Full contract and SDK smoke suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest tests/test_agent_runtime.py tests/test_studio_contract_registry.py tests/test_studio_planner_factory.py tests/test_chiploop_sdk_cli.py
```

## Known Limitations

- `run_workflow()` preserves the existing multipart backend contract; workflow names still need compatible backend workflow payloads or inputs.
- The SDK does not introduce DAG execution, parallel execution, deployment, or IDE integration.
- Authentication behavior depends on the backend token policy already configured for the target environment.

## Next Phase Notes

- Add packaged SDK publishing only after the local API is stable.
- Add richer workflow template helpers once workflow contracts move beyond metadata.
- Add IDE integration as a separate phase.
