# Drydock

Drydock provisions, connects, and governs the sandboxed workspaces where Claude agents live and do work.

**Status:** V1 shipped — host-side CLI over devcontainer primitives.

**Quick start:** See [docs/getting-started.md](docs/getting-started.md).

## Repo layout

```
.devcontainer/     Workspace template (Dockerfile, firewall, Tailscale, remote control)
base/              Published base image (drydock-base) — build and push scripts
src/drydock/       The ws CLI (Python/Click)
  cli/             Commands: create, list, inspect, stop, destroy, attach
  core/            Registry (SQLite), workspace model, devcontainer wrapper, checkout, overlay
  output/          JSON and human-readable formatting
docs/              Canonical design docs and specs
tests/             pytest suite
```

## The idea

Drydock is a personal agent fabric. Each workspace is a durable, addressable place where an agent works — not a throwaway container. It has a stable name, a scoped firewall policy, a git checkout, and a Claude Code session you can reach from anywhere on your tailnet. The container can come and go; the workspace persists.

V1 ships as a CLI (`ws`) that runs on the host. The long-term shape is a daemon-mediated control plane with policy graph, audit, secrets brokering, and cross-host placement.

## Further reading

- [docs/vision.md](docs/vision.md) — what this is becoming
- [docs/v2-scope.md](docs/v2-scope.md) — daemon design and roadmap
