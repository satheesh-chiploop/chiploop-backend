# Run Arch2RTL from the ChipLoop CLI

Set credentials:

```bash
set CHIPLOOP_BASE_URL=https://your-backend.example
set CHIPLOOP_API_KEY=cl_live_...
```

Check setup:

```bash
chiploop doctor
chiploop workflows list
```

Run Arch2RTL:

```bash
chiploop run arch2rtl --spec examples/sdk/arch2rtl_spec.md
```

Poll status:

```bash
chiploop status <workflow_id>
```

List and download artifacts:

```bash
chiploop artifacts list <workflow_id>
chiploop artifacts download <workflow_id> --name rtl/top.v --out ./outputs
```

Automation mode:

```bash
chiploop workflows list --json
chiploop plan --json
chiploop usage --json
```
