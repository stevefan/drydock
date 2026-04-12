# Drydock — Vision

## What it is

Drydock is the infrastructure layer for personal agent workspaces. It launches, tracks, connects, and manages containerized development environments across projects, machines, and sessions.

It's a workspace control plane — not a container template, not an IDE, not a CI system. Projects define their own environments (via devcontainer.json). Drydock orchestrates them: resolves secrets, applies network policy, registers the workspace, and makes it reachable from anywhere.

## Where it sits in the stack

```
Substrate    — semantic layer: hypergraphs, dialogue, LLM-native knowledge processing
Patchwork    — context layer: life data (transcripts, tweets, finance, search)
Drydock      — infrastructure layer: where things run, how you connect, workspace lifecycle
```

Substrate ingests and processes. Patchwork stores and indexes. Drydock runs and connects. Each project (Substrate, Patchwork, ASI, Microfoundry, others) is a workspace that Drydock can spawn, manage, and provide resources to.

## What it does

1. **Workspace lifecycle** — create, fork, stop, resume, destroy workspaces across projects. One command to go from "I want to work on Patchwork" to a running, firewalled, network-connected container with the right secrets.

2. **Resource brokering** — secrets mounted at `/run/secrets/`, per-workspace firewall policy, Tailscale networking. The single place that knows what each workspace needs and provides it.

3. **Host management** — workspaces run locally or on remote machines. Your laptop is a control surface, not necessarily the compute. Workspaces keep running when you close your laptop.

4. **Connectivity** — attach from laptop, phone, another Claude session. SSH, tmux, Claude Code remote control, browser terminal. The workspace is reachable regardless of how you're connecting.

5. **Registry** — what's running, where, on what branch, in what state. The fleet view across all projects and hosts.

## What it doesn't do

- **Define project environments** — that's the project's devcontainer.json. Drydock reads it, doesn't duplicate it.
- **Process data or run application logic** — that's Substrate, Patchwork, etc.
- **Provision cloud accounts or manage DNS** — manual for now, light automation later if the friction justifies it.

## How it relates to devcontainers

Projects own their `.devcontainer/devcontainer.json`. This works standalone with VS Code "Reopen in Container." Drydock is an alternative launch path that layers orchestration on top via `devcontainer up --override-config`.

Two launch paths, no conflict:

| Path | What happens |
|---|---|
| VS Code "Reopen in Container" | Uses project's devcontainer.json directly. No Drydock involved. |
| `ws create <project>` | Drydock merges project devcontainer + orchestration overlay. Managed lifecycle. |

The per-project Drydock config captures only what devcontainer.json can't express: secrets policy, firewall rules, Tailscale hostname, workspace metadata.

## How it relates to Docker

```
ws CLI → devcontainer CLI → Docker/Podman
```

Drydock never talks to Docker directly. Podman support comes free from the devcontainer CLI. Drydock manages workspaces — the abstraction that connects a project, a git branch, a container, and the work happening inside it. Docker just runs containers.

## The agent angle

Claude operates at two levels:
- **Operator** — you (or Claude on your host) call `ws create` to provision workspaces.
- **Occupant** — Claude runs inside each workspace as the development agent, accessible via remote control.

Each workspace is a self-contained Claude agent environment: code, tools, network access, and a remote control endpoint. You check in from wherever you are — laptop, phone, another Claude session. Drydock is the workshop where you outfit the ships.

## Network and firewall

The existing default-deny firewall (iptables/ipset) and Tailscale integration are Drydock's foundation. Whether network setup lives in Drydock's overlay or in project devcontainers is still being worked out — but the capability is Drydock's responsibility either way. Projects shouldn't have to think about firewalling or tailnet attachment.

## Architecture

The `ws` CLI runs on the host — your laptop or a server. It is not containerized. Containers are workspaces, not orchestrators.

```
Host machine
├── ws CLI (pip install)
├── devcontainer CLI
├── Docker
├── ~/.drydock/registry.db
└── /srv/secrets/
         │
         │  ws create substrate
         ▼
    ┌─────────────────────────┐
    │ Workspace container      │
    │  Claude Code + remote    │
    │  iptables firewall       │
    │  Tailscale networking    │
    │  project code mounted    │
    └─────────────────────────┘
```

The control plane is just a CLI and a SQLite file. No daemon, no server, no container-in-container. If remote orchestration becomes valuable later (scheduling workspaces across machines, API access), `ws` can grow a server mode — but v1 is a local CLI.

### Components

- **`ws` CLI** — Python, installed on host, wraps `devcontainer` CLI
- **Registry** — SQLite at `~/.drydock/registry.db`, tracks workspace state
- **Workspace template** — the `.devcontainer/` in this repo: Dockerfile with Claude, firewall, Tailscale, remote control. This is what gets built when a project doesn't have its own devcontainer.
- **Override generator** — `ws create` produces a devcontainer override JSON that layers orchestration (identity, secrets, networking) onto any project's devcontainer
- **Per-project config** — YAML in `drydock/projects/`, orchestration delta only
- **Secrets** — resolved from `/srv/secrets/`, mounted at `/run/secrets/`
- **Remote hosts** — SSH + devcontainer CLI on remote machines on the tailnet (future)

## Why it matters

Workspaces are persistent places, not ephemeral sessions. You don't "start working on Patchwork" — you check in on the Patchwork workspace. Claude is a coworker with a desk, not a tool you invoke. It has a persistent session with context, history, and ongoing tasks. You can leave, come back, and ask "what did you do while I was gone?"

The firewall is what makes autonomous agent work safe. Default-deny means Claude literally cannot reach services you haven't approved. It can't accidentally hit production APIs, can't leak code to unauthorized endpoints. The sandbox is what lets you close your laptop and trust the work continues.

Tailscale makes location irrelevant. A workspace has a stable tailnet hostname. You think in workspace names, not machine names. You check in from your phone, your laptop, or another Claude session — it doesn't matter.

## Network policy

Conservative default: your devices can reach workspaces, workspaces can reach their internet whitelist, workspaces don't talk to each other.

Drydock owns both layers of network policy:
- **Tailscale ACLs** — tailnet-wide rules controlling which tagged nodes can reach which. Workspace containers get a `tag:workspace` tag. Personal devices get `tag:personal`.
- **iptables on tailscale0** — per-container rules restricting which ports are exposed and what the container can initiate over the tailnet. No blanket `ACCEPT` on tailscale0.

Cross-workspace communication (e.g., Substrate reading from Patchwork) is a future capability, gated by explicit policy.

## v1 scope

The v1 mission: infrastructure and Claude that you can talk to and work on without worrying about your laptop being closed.

**In scope:**
- `ws create <project>` — local or remote (SSH host)
- `ws list` / `ws inspect` / `ws attach` / `ws stop`
- Secrets resolution and `/run/secrets/` mount
- Firewall with per-project domain whitelist
- Tailscale with conservative ACLs and locked-down tailscale0
- Claude Code remote control running in each workspace
- SQLite registry tracking workspace state
- Per-project YAML config (orchestration delta only)

**Out of scope for v1:**
- Agent-to-agent coordination
- Cross-workspace networking
- Workspace forking
- Tailscale ACL management UI
- Host provisioning automation
- Mobile dashboard
- Substrate as task/conversation coordination medium

## What this is not

Not a general platform. Not multi-tenant. Not trying to replace Kubernetes or GitHub Actions. It's a personal workspace control plane for someone who runs multiple projects with agent assistance across multiple machines. The right amount of infrastructure for that specific problem.
