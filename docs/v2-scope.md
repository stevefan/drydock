# Drydock v2 — `ws` daemon for nested orchestration

**Status:** design sketch, 2026-04-12
**Motivated by:** [microfoundry nested orchestration](requirement-microfoundry-nested-orchestration.md)

## The problem v2 solves

V1's control plane is a CLI that runs on the host. That works for "Steven at the keyboard" but fails the moment a Claude agent *inside* a workspace wants to spawn a sibling workspace.

The wrong answer is mounting the Docker socket into agent workspaces. That collapses the blast radius to the whole fleet and contradicts Drydock's default-deny posture.

The right answer is a **host-side `ws` daemon** that workspaces talk to over an authenticated channel. The daemon is the explicit orchestration surface; workspaces ask permission, the daemon enforces policy.

## Principles v2 preserves

- **Layering:** tools that manage infrastructure live outside the thing they manage. Workspaces stay ignorant of Docker; the daemon mediates.
- **Default-deny:** a parent workspace cannot grant a child any capability the parent does not already hold. Children are *strictly narrower* than parents.
- **Explicit capability grants:** no workspace is "permissionless." Each gets enumerated capabilities; spawning is one of them.
- **No daemon on the critical path for single-user local use:** `ws` still works as today's local CLI when invoked on the host. The daemon is additional plumbing, not a replacement.

## Architecture

```
Host
├── ws CLI (host mode)                              — direct: ws → registry + devcontainer CLI
├── ws daemon (wsd)                                 — HTTP/Unix socket, authenticated
├── registry.db (now with parent-child column)
└── Docker
       │
       │  ws create myproject  (from host)
       ▼
  ┌──────────────────────────────────────┐
  │ Workspace "myproject"                 │
  │                                       │
  │   ws CLI (workspace mode)             │
  │     │ detects in-container context    │
  │     │ routes to daemon via Tailscale  │
  │     ▼                                 │
  │   HTTP → wsd → registry, devcontainer │
  └──────────────────────────────────────┘
```

### Components

| Component | Role |
|---|---|
| `ws` CLI (host mode) | Unchanged from v1 — writes registry directly, calls devcontainer CLI directly |
| `ws` CLI (workspace mode) | Detects `$DRYDOCK_WORKSPACE_ID` env, routes commands to daemon over HTTP instead of executing locally |
| `wsd` (new) | Host-side daemon; enforces policy, writes to registry, invokes devcontainer CLI on behalf of authorized callers |
| Parent-child registry | New columns on workspaces: `parent_workspace_id`, `delegated_capabilities` |
| Policy validator | Pure function: given a parent's policy and a child's requested policy, return `allow` or `reject with reason` |

## Protocol sketch

Workspace → daemon over Tailscale, authenticated:

```
POST /v2/workspaces                     — ws create (child)
GET  /v2/workspaces?parent=<id>         — ws list (children of parent)
POST /v2/workspaces/<name>/stop         — ws stop
DELETE /v2/workspaces/<name>            — ws destroy
```

Each request carries the caller's workspace id (set in-container via `DRYDOCK_WORKSPACE_ID`) and an auth token. The daemon looks up the caller's capabilities in the registry before dispatching.

### Auth model — open question

Candidates:

- **Bearer token per workspace:** daemon issues a token at `ws create`, mounted into the workspace at `/run/secrets/drydock-token`. Simple, rotatable, but the token is a capability — steal it and you're the workspace.
- **mTLS via Tailscale device identity:** tailnet authenticates the connection; daemon maps tailnet node to workspace id. Stronger, but couples v2 to Tailscale as more than just the transport.
- **Unix socket with peer credentials:** only works when daemon and workspace share a host (probably true for now). Simplest if the remote-host case is deferred.

**Recommendation:** start with bearer tokens on Tailscale. Revisit if the token model stresses.

## Policy validation — the core guarantee

Before spawning a child, the daemon must verify:

1. **Firewall narrowness:** `child.firewall_extra_domains ⊆ parent.firewall_extra_domains ∪ parent.delegatable_firewall_domains`. A child cannot reach a domain the parent cannot.
2. **Secret narrowness:** `child.secrets ⊆ parent.delegatable_secrets`. Parent explicitly declares which of its secrets it may delegate; children can request a subset.
3. **Capability narrowness:** `child.capabilities ⊆ parent.capabilities`. If a parent cannot spawn grandchildren, nor can its children (no lateral capability expansion).
4. **Resource limits:** parent has a budget (count of children, total CPU/memory); spawning a child debits it.

The validator is a pure function. Write it with lots of tests; it is the trust boundary.

## Registry schema additions

New columns on `workspaces`:

| Column | Type | Purpose |
|---|---|---|
| `parent_workspace_id` | TEXT NULL | null for host-created workspaces; set to parent's id for daemon-spawned children |
| `delegatable_firewall_domains` | TEXT (JSON list) | domains this workspace may grant to children |
| `delegatable_secrets` | TEXT (JSON list) | secret keys this workspace may grant to children |
| `capabilities` | TEXT (JSON list) | explicit capability grants (e.g. `["spawn_children", "edit_monorepo"]`) |

`destroy` cascades: destroying a parent destroys its children first.

## CLI routing logic

The same `ws` binary does the right thing in both contexts:

```python
# pseudocode
if os.environ.get("DRYDOCK_WORKSPACE_ID"):
    # workspace mode — route to daemon
    daemon_client.dispatch(sys.argv)
else:
    # host mode — direct, as today
    main_cli()
```

`DRYDOCK_WORKSPACE_ID` is already set by the overlay (iteration 1 work). Picking it up in the CLI router is small.

## What's in v2 scope

- `wsd` daemon (HTTP/Unix socket + bearer auth over Tailscale)
- CLI routing for in-workspace invocation
- Policy validator module with strong test coverage
- Registry schema additions
- `--parent` flag for `ws create`
- `destroy` cascade semantics

## What's explicitly out of v2 scope

- **Cross-host orchestration.** V2 is still single-host. Workspaces on separate machines is a later milestone.
- **Capability UI / audit tooling.** CLI/log-based for now.
- **Automatic policy derivation.** Parents declare their `delegatable_*` stanzas explicitly; the daemon does not infer them.
- **Web dashboard.** Not now.
- **Agent-to-agent coordination** beyond spawn/destroy. Siblings still don't talk to each other over the tailnet by default.

## Open questions (for dedicated v2 design session)

1. Where does the daemon live — a launchd service on the host? A tmux pane? How does it get restarted on boot?
2. How does the daemon handle devcontainer CLI errors — retry, propagate, mark workspace as error?
3. Does `ws attach` (host → workspace) route through the daemon too, or stay direct?
4. How does workspace-local state (e.g. in-progress tasks) survive a daemon restart?
5. What's the audit trail — every daemon action logged to `~/.drydock/audit.log`?
6. Capability revocation: if a parent's policy changes, do running children get their capabilities re-evaluated?

## Migration from v1

- V1 workspaces (no `parent_workspace_id`) continue to work unchanged — they're host-spawned and the daemon ignores them
- New `ws` version ships with CLI routing logic but defaults to host mode unless daemon is configured
- Daemon is opt-in; if you never start `wsd`, v1 behavior is preserved

## When to build this

When microfoundry's nested case becomes painful enough that spawning children from the host feels wrong. That's the signal — not "we have a clever design, let's build it." V2 is infrastructure, and infrastructure should follow demonstrated need.
