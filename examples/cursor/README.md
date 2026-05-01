# Using ChipLoop in Cursor

Cursor users can use ChipLoop through the terminal today. This keeps the flow simple and uses the same public SDK/CLI path as VS Code and GitHub Actions.

## Setup

```bash
pip install chiploop-sdk
```

Set environment variables in your shell, not in source files:

```bash
set CHIPLOOP_BASE_URL=https://api.chiploop.com
set CHIPLOOP_API_KEY=cl_live_...
```

## Run Arch2RTL

```bash
chiploop doctor
chiploop run arch2rtl --spec specs/pwm.md
```

Then:

```bash
chiploop status <workflow_id>
chiploop artifacts list <workflow_id>
chiploop artifacts download <workflow_id> --name rtl/top.v --out ./chiploop_outputs
```

## Suggested Cursor Workflow

1. Keep specs in `specs/`.
2. Run ChipLoop from the Cursor terminal.
3. Download artifacts into `chiploop_outputs/`.
4. Review generated RTL, SDC, UPF, and reports.
5. Use Cursor to inspect, explain, or modify local files.

## Safety Rules

- Do not commit API keys.
- Do not auto-commit generated RTL without review.
- Keep generated outputs in a review folder until approved.
- Use Pro, Pro Max, or Enterprise credentials for SDK/CLI automation.
