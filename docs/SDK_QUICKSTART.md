# ChipLoop SDK Quickstart

## Install

Local editable install during development:

```bash
pip install -e .
```

Published package target:

```bash
pip install chiploop-sdk
```

## Configure

```bash
set CHIPLOOP_BASE_URL=https://your-backend.example
set CHIPLOOP_API_KEY=cl_live_...
```

## Python

```python
from chiploop_sdk import ChipLoopClient

client = ChipLoopClient()
run = client.run_workflow("arch2rtl", spec_text="Create a simple PWM controller.")
print(run.workflow_id)
```

## CLI

```bash
chiploop doctor
chiploop workflows list
chiploop run arch2rtl --spec examples/sdk/arch2rtl_spec.md
```

## Useful Commands

```bash
chiploop agents list
chiploop status <workflow_id>
chiploop artifacts list <workflow_id>
chiploop usage
chiploop plan
```

