# ChipLoop VS Code Extension

This is the Phase 14 MVP scaffold for running ChipLoop from VS Code.

The extension reuses the public `chiploop` CLI instead of calling browser-only endpoints. This keeps behavior aligned with SDK/CLI access and avoids duplicating API logic in TypeScript.

## Prerequisites

Install the CLI:

```bash
pip install chiploop-sdk
```

SDK/CLI access is intended for Pro, Pro Max, and Enterprise plans.

## Commands

From the VS Code Command Palette:

```text
ChipLoop: Configure
ChipLoop: Run Workflow from Current File
ChipLoop: Run Arch2RTL
ChipLoop: Check Workflow Status
ChipLoop: Download Artifacts
ChipLoop: Open Apps
ChipLoop: Open Studio
```

## Configuration

Settings:

```json
{
  "chiploop.baseUrl": "https://api.chiploop.com",
  "chiploop.defaultWorkflow": "arch2rtl",
  "chiploop.outputDir": "chiploop_outputs"
}
```

The API key is stored in VS Code SecretStorage through `ChipLoop: Configure`.

## MVP Behavior

1. Open a spec file.
2. Run `ChipLoop: Run Arch2RTL`.
3. Check status from the Command Palette.
4. Download artifacts into the workspace.
5. Open generated RTL, SDC, UPF, or reports locally.

## Not Included Yet

- Marketplace install from VS Code
- Visual workflow editor
- Automatic commits
- Registry edits
- Auto-promotion of generated agents
