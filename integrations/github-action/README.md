# ChipLoop GitHub Action

This MVP action runs a ChipLoop workflow from GitHub Actions by reusing the public `chiploop` CLI.

It does not call browser-only endpoints and does not write secrets to logs.

## Required Secrets

Configure repository or organization secrets:

```text
CHIPLOOP_BASE_URL
CHIPLOOP_API_KEY
```

SDK/CLI access is intended for Pro, Pro Max, and Enterprise plans.

## Example

```yaml
name: ChipLoop Arch2RTL

on:
  workflow_dispatch:
  pull_request:
    paths:
      - "specs/**"

jobs:
  arch2rtl:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: ./backend/integrations/github-action
        with:
          workflow: arch2rtl
          spec: specs/pwm.md
          output-dir: chiploop_outputs
          wait: "true"
        env:
          CHIPLOOP_BASE_URL: ${{ secrets.CHIPLOOP_BASE_URL }}
          CHIPLOOP_API_KEY: ${{ secrets.CHIPLOOP_API_KEY }}

      - uses: actions/upload-artifact@v4
        with:
          name: chiploop-artifacts
          path: chiploop_outputs
```

## MVP Scope

- Install `chiploop-sdk`
- Run `chiploop doctor`
- Run a workflow from spec file or inline spec text
- Poll status
- Download artifacts into a workspace directory

## Not Included Yet

- GitHub App installation
- Pull request comments
- Check annotations
- Auto-committing generated files
- Auto-merge or registry promotion
