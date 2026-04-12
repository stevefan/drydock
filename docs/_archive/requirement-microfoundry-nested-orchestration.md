# Requirement: Microfoundry nested agent orchestration

**Status:** committed requirement, 2026-04-12
**Forcing function for:** Drydock v2 (`ws` daemon — see [v2-scope.md](v2-scope.md))

## The use case

Microfoundry is a monorepo with heterogeneous sub-projects whose isolation needs differ by at least an order of magnitude:

| Sub-project | Stack | Isolation need |
|---|---|---|
| `microfluidics-dsl` | Python (CadQuery) | Low — pure compute |
| `fluid-cad` | Rust (PDE solver, Z3) | Low — pure compute |
| `mold-template` | Python (CadQuery) | Low — pure compute |
| `3duf` | Node / web app | Medium — dev server, browser |
| `auction-crawl` | Python + Playwright | **High** — browser automation hitting arbitrary auction sites, pulls untrusted HTML |
| `confocal-microscope` | Config files (Zeiss) | N/A — data only |

The requirement: a top-level Claude running in the microfoundry workspace that can

1. Edit files across all sub-projects (monorepo-wide refactors, coordinated changes)
2. Spawn per-sub-project child agents for tool-specific work (Playwright crawls, CadQuery exports, Rust tests)
3. Confine high-risk sub-projects (`auction-crawl`'s browser automation) so a compromised listing can't reach anything but the whitelisted auction hosts

This maps onto Drydock cleanly: the top-level Claude runs in a workspace with edit authority and *orchestration authority*; per-sub-project child agents run as separate `ws create`d workspaces with their own narrower firewall policies and selective secret inheritance.

## What this tests about Drydock's design

### 1. Nested workspace spawning

V1 Drydock assumes `ws create` runs on the host. This requirement adds a second caller: **a Claude agent inside a Drydock workspace calling `ws create` to spawn a child workspace**.

**Mechanism — committed decision:** a host-side `ws` daemon, *not* Docker-socket-mount on the parent container.

- Parent workspace talks to the daemon over an authenticated channel (Tailscale + token)
- Daemon validates: is this parent authorized to spawn? Is the child's policy strictly narrower than the parent's?
- Parent never touches Docker directly; it asks permission
- Registry tracks parent-child relationships so `destroy` can cascade

This preserves the layering principle ("tools managing infrastructure live outside the thing they manage") while enabling nesting. See [v2-scope.md](v2-scope.md) for the architectural plan.

**Why not Docker socket mount on the parent:**
- Socket mount = parent has root on host Docker daemon = blast radius is the entire fleet
- Contradicts Drydock's default-deny network posture with a default-allow capability
- One compromised child could operate any container on the host, including non-children

### 2. Per-sub-project devcontainer vs. monorepo-root devcontainer

Drydock already says projects own their `devcontainer.json`. The monorepo case needs *both*:

- **Monorepo root:** lightweight devcontainer for the top-level Claude — edit authority + daemon-client for spawning children. Used by `ws create microfoundry`.
- **Per-sub-project:** heavier devcontainers for tool-specific work (e.g. `auction-crawl/.devcontainer/` with Playwright browsers baked in). Used when spawning a sub-project workspace.

This suggests the CLI supports a `--parent` flag for nesting (`ws create --parent microfoundry auction-crawl`) rather than slash-pathed project names.

### 3. Risk-tiered firewall policies

`auction-crawl` needs to hit a handful of specific auction hosts and the Anthropic API. It must not be able to reach anything else.

This is already supported by per-project YAML (`drydock/projects/auction-crawl.yaml` with a narrow `firewall_extra_domains`). The v2 daemon's policy validator must refuse to spawn a child whose declared policy is *broader* than the parent's, so a compromised parent can't laterally expand its own authority.

### 4. Selective secret inheritance

Top-level Claude needs `ANTHROPIC_API_KEY`. Children may or may not — `auction-crawl` needs it (for the classifier); `fluid-cad` doesn't.

Children declare the secrets they need in their project YAML. The v2 daemon honors only the subset the parent is allowed to delegate; nothing is inherited implicitly.

## What v1 already delivers for microfoundry

Microfoundry can start using Drydock *today* — from the host, not from inside another workspace:

- `ws create microfoundry` from the host launches the top-level microfoundry workspace
- `ws create auction-crawl` from the host launches a sibling workspace for auction-crawl
- Each workspace gets its own per-project YAML with firewall/secrets policy
- No nested spawning yet; all orchestration happens from the host

This is enough to validate the isolation model, firewall policy differentiation, and per-project config flow. The nested-spawn story is v2.

## Concrete next steps

1. Write `drydock/projects/microfoundry.yaml` and `drydock/projects/auction-crawl.yaml` describing the policy each workspace needs
2. Add `.devcontainer/` to each microfoundry sub-project that warrants its own isolated environment
3. Dogfood `ws create` for microfoundry from the host; iterate on the YAML schema as real friction surfaces
4. When v1 feels solid, design and build the v2 daemon (see [v2-scope.md](v2-scope.md))

## Why log this

Microfoundry is the first case where Drydock's "monorepo with heterogeneous sub-projects needing differentiated isolation" design gets real pressure. Substrate and Patchwork are more homogeneous. If Drydock handles microfoundry well, the design generalizes; if it doesn't, microfoundry is the forcing function.
