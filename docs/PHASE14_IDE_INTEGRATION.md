# Phase 14 IDE Integration

Phase 14 introduces developer workflow integrations without changing backend workflow execution.

## Goal

Meet users where they work:

- VS Code
- Cursor
- GitHub Actions

These integrations reuse the public SDK/CLI surface and `/sdk/v1` APIs. Browser-only endpoints remain browser-only.

## Plan Access

Developer automation is intended for:

- Pro
- Pro Max
- Enterprise

Starter remains browser-first with Apps and basic Studio access.

## Added MVP Scaffolds

### GitHub Action

Path:

```text
integrations/github-action
```

Capabilities:

- install `chiploop-sdk`
- run `chiploop doctor`
- run a workflow from a spec file or inline spec text
- poll status
- download artifacts

Secrets:

```text
CHIPLOOP_BASE_URL
CHIPLOOP_API_KEY
```

### VS Code Extension

Path:

```text
integrations/vscode-chiploop
```

Capabilities:

- configure base URL and API key
- store API key in VS Code SecretStorage
- run the current spec file
- run Arch2RTL
- check workflow status
- download artifacts
- open Apps or Studio

The extension shells out to the existing `chiploop` CLI.

### Cursor

Path:

```text
examples/cursor/README.md
```

Cursor is supported through terminal-based CLI usage for now.

## What Was Not Added

- no backend workflow changes
- no deployment
- no GitHub App
- no PR comments
- no auto-commit
- no registry editing from IDE
- no marketplace install from IDE
- no Cursor-specific plugin
- no MCP server

## Next Steps

1. Validate CLI/SDK with users.
2. Try the GitHub Action on an internal repo.
3. Package the VS Code extension locally for testing.
4. Add PR summaries only after the GitHub Action is proven.
5. Consider a VS Code sidebar after Command Palette usage is validated.
