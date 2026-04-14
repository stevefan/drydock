---
description: Manage Drydock workspaces — create, list, inspect, stop, destroy agent containers
allowed-tools: Bash(ws *)
---

# ws — Drydock Workspace CLI

You are managing workspaces via the `ws` CLI. Output is JSON when piped (which is how you see it). Errors include a `fix` field with the corrective action.

## Commands

```
ws create <project> [name]        Create a workspace (name defaults to project)
ws list [--project P] [--state S] List workspaces
ws inspect <name>                 Full workspace details
ws stop <name>                    Stop a running workspace
ws destroy <name> --force         Remove a workspace (requires --force)
```

## Global flags

```
ws --json <command>     Force JSON output
ws --dry-run <command>  Preview what would happen without doing it
```

## Common workflows

**Create a workspace:**
```bash
ws create myproject
```

**Create for a specific user:**
```bash
ws create myproject --owner alice
```

**Check what's running:**
```bash
ws list --state running
```

**Preview a destructive action:**
```bash
ws --dry-run destroy old-workspace
```

**Then execute it:**
```bash
ws destroy old-workspace --force
```

## Error handling

All errors return JSON with `error` and `fix` fields:
```json
{"error": "Workspace 'foo' not found", "fix": "Run 'ws list' to see available workspaces"}
```

Read the `fix` field and follow its instruction. Do not guess or retry without reading the error.

## Notes

- `ws create` is idempotent — if the workspace exists, it reports the conflict and tells you what to do
- `ws destroy` requires `--force` — always use `--dry-run` first to preview
- Output is always JSON in non-TTY contexts (which is how you invoke it)
