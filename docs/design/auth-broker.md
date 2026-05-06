# Auth broker

**Status:** sketch · **Depends on:** [capability-broker.md](capability-broker.md), [in-desk-rpc.md](in-desk-rpc.md), [tailnet-identity.md](tailnet-identity.md). The auth-broker is one of the Authority's responsibilities — credential-refresh enforcement on a designated Harbor, deterministically.

## Problem

Claude Code's OAuth credentials decay. The remote-control process inside a drydock refreshes its access token in memory but doesn't write back to `~/.claude/.credentials.json`, so file consumers (other desks pulling `claude_credentials` via `RequestCapability`, init scripts bind-mounting `/run/secrets/`) go stale every ~8h. Today's mitigation is a launchd cron on Steven's Mac (`scripts/mac/claude-refresh.sh`) that warms the keychain, extracts fresh tokens, and ssh-pushes them to a configured list of `Harbor:desk` pairs.

This breaks in two ways:

1. **Laptop closed → no refresh.** The Mac keychain is the only source of truth, and the cron only fires when Mac is awake. Overnight the archipelago drifts to expired tokens.
2. **Initial login on headless remotes is awful.** `claude /login` wants an interactive browser + code paste. The tmux/OSC52 backup works but is per-Harbor manual.

## Goal

A designated **auth Harbor** (default: the always-on Hetzner box) holds the master refresh token, periodically mints fresh access tokens, and distributes credentials to peers — without requiring Steven's Mac to be awake. Initial logins can be performed from a phone over tailnet against the auth Harbor's OAuth callback endpoint.

## Design

### Roles

- **Auth Harbor** — one designated Harbor per identity (Steven's personal account; ASI account is separate). Holds `claude_master_refresh_token` in `~/.drydock/daemon-secrets/`. Runs the refresh loop. Exposes the OAuth callback listener on tailnet.
- **Peer Harbor** — every other Harbor. Receives credential pushes from the auth Harbor over tailnet RPC. Has no refresh capability of its own.
- **Mac (optional, recommended)** — secondary refresh source. Continues running `claude-refresh.sh` as a fallback / drift detector. If Mac and auth Harbor disagree on refresh-token generation, Mac wins (keychain is canonical).

### Refresh loop (steady state)

On the auth Harbor, a `drydock daemon`-managed timer fires every 4h:

1. Read `claude_master_refresh_token` from daemon-secrets.
2. POST to Anthropic's OAuth token endpoint with `grant_type=refresh_token`. **Empirical unknown:** the exact endpoint URL, client_id, and whether the response rotates the refresh token. Validate by capturing `claude` CLI's network traffic on a deliberate refresh, before writing the implementation.
3. On success: write the new `{access_token, refresh_token, expires_at}` blob to local `claude_credentials` secret slot, plus push to every peer Harbor's matching drydocks via the existing capability-broker `LeaseSecret` flow (auth Harbor acts as the holder; peers request leases).
4. If refresh-token rotated, atomically replace `claude_master_refresh_token` and log the rotation.
5. On failure (refresh token revoked, network down): log + emit a deskwatch violation. Do NOT delete the existing token.

### OAuth callback over tailnet

For initial login from anywhere (phone, new laptop, friend's machine):

1. User runs `drydock auth login --on <auth-harbor>` from any device. CLI prints an Anthropic authorize URL with `redirect_uri=http://<auth-harbor>.tailnet:8443/oauth/callback`.
2. User opens URL on phone. Phone's browser, on tailnet, follows the Anthropic redirect to the auth Harbor.
3. Auth Harbor's listener (a small HTTPS endpoint served by `drydock daemon`, or a sidecar) receives `?code=...`, exchanges with Anthropic for tokens, stores as `claude_master_refresh_token`.
4. Refresh loop kicks immediately to seed peers.

**Critical unknown:** does Claude Code's OAuth client accept arbitrary `redirect_uri` values, or is it pinned to `http://localhost:<port>`? Three fallbacks if pinned:
  - **Tunnel:** phone runs a Tailscale Funnel/SSH tunnel from `localhost:PORT` → `auth-harbor:PORT`. Phone-browser hits localhost; tunnel forwards. Works but requires tunnel setup on phone.
  - **`/etc/hosts` spoof:** auth Harbor maps a CC-expected hostname to itself, listens on the expected port. Brittle.
  - **Code-paste:** phone visits authorize URL, gets code, user types into `drydock auth submit-code <code> --on <auth-harbor>`. Always works; needs typing.

Decision: ship code-paste path first (zero-unknowns), then tailnet redirect once the OAuth client behavior is verified.

### CLI surface

| Command | Purpose |
|---|---|
| `drydock auth designate <harbor>` | Mark a Harbor as the auth Harbor for the current identity. Writes to local config. |
| `drydock auth seed --from-keychain` | One-time: extract refresh token from Mac keychain, push to auth Harbor's daemon-secrets. |
| `drydock auth login --on <harbor>` | Print authorize URL; on auth Harbor, start callback listener (5min window). |
| `drydock auth submit-code <code> --on <harbor>` | Submit OAuth code from out-of-band browser. |
| `drydock auth status` | Show last-refresh time, expiry, peer push status across the archipelago. |
| `drydock auth refresh --on <harbor>` | Force a refresh now (debugging). |

### Defaults to make this possible

- **Auth Harbor identity:** default to the Hetzner Linux Harbor. One per OAuth identity (personal vs ASI). Recorded in `~/.drydock/auth-harbor.conf`.
- **Refresh interval:** every 4h (well under the 8h decay window). Add jitter to avoid thundering herd if multiple identities ever share a clock.
- **Peer discovery:** reuse `claude-refresh.conf` format (`harbor:desk` pairs) for v1. Migrate to "ask each peer Harbor what drydocks need `claude_credentials`" once `drydock daemon` peer-RPC is in place.
- **Mac role:** Mac stays a **co-refresher** for redundancy. If Mac pushes a different refresh token than the auth Harbor's loop, the Mac push wins (keychain is canonical for OAuth client identity). Logged as a rotation event.
- **Failure mode:** if refresh fails 3× consecutive, deskwatch violation + Telegram alert (see harbor-monitor design). Keep the last-known-good token.
- **Daemon-secrets isolation:** `claude_master_refresh_token` lives in `~/.drydock/daemon-secrets/` (root:0600), NOT a per-drydock secret. Only `drydock daemon` reads it. Peers never see the refresh token, only access tokens.

### Security

- The auth Harbor is now a high-value target: compromise = full Claude account takeover. Mitigations: refresh token never leaves the auth Harbor; peers receive only short-lived access tokens; tailnet ACL restricts the callback port; audit every refresh + every push.
- `drydock auth seed --from-keychain` is the only operation that exposes the refresh token in transit. Use SSH-over-tailnet, never plain network.
- ASI identity gets its own auth Harbor (or co-locates with hard isolation) per the existing `aws_credential_topology` discipline.

## Open questions

1. Anthropic OAuth token endpoint contract (URL, client_id, refresh-token rotation behavior). Verify empirically before implementing.
2. Does CC's OAuth client accept non-localhost `redirect_uri`? Determines whether the tailnet-redirect or code-paste path is primary.
3. Should `claude_account_state` (`~/.claude.json`) also be brokered, or remain a per-Harbor static file? Today it's pushed alongside credentials but rarely changes.
4. Migration: how do peers re-key when a refresh-token rotation happens? Current design assumes immediate push to all peers; consider a pull-on-demand variant where peers request fresh creds via `RequestCapability` and `drydock daemon` mints them.
