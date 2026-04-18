# Systemd units (Linux Harbors)

`scripts/install-linux-services.sh` installs two units and two helper scripts. Together they make `systemctl restart drydock-wsd` transparent to running drydocks and `reboot` self-heal for every drydock that was running.

Design rationale in [../design/persistence.md](../design/persistence.md). macOS equivalent is `base/com.drydock.wsd.plist` (launchd user agent).

## What installs where

| Source | Target | Mode |
|---|---|---|
| `base/drydock-wsd.service` | `/etc/systemd/system/drydock-wsd.service` | 0644 |
| `base/drydock-desks.service` | `/etc/systemd/system/drydock-desks.service` | 0644 |
| `scripts/drydock-resume-desks` | `/usr/local/bin/drydock-resume-desks` | 0755 |
| `scripts/drydock-stop-desks` | `/usr/local/bin/drydock-stop-desks` | 0755 |
| `scripts/drydock-rpc` | `/usr/local/bin/drydock-rpc` **and** `/root/.drydock/bin/drydock-rpc` | 0755 |

`ws host init` also deploys `drydock-rpc` to `~/.drydock/bin/` — the overlay bind-mount source path — so creating a drydock picks it up without running the install script.

## Units

**`drydock-wsd.service`** — long-running daemon, `Restart=on-failure`. Runs as root. Binds `~/.drydock/run/wsd.sock` and chmods it `0o666` so container uid 1000 can connect (bearer token is the real security gate — see [../design/in-desk-rpc.md](../design/in-desk-rpc.md)). Logs append to `/root/.drydock/logs/wsd-systemd.log`.

**`drydock-desks.service`** — `Type=oneshot`, `RemainAfterExit=yes`, `After=drydock-wsd.service`, `Requires=drydock-wsd.service`. Two hooks:
- `ExecStart=/usr/local/bin/drydock-resume-desks` — poll `ws daemon status`, then `ws create <name>` for every drydock whose registry state is `suspended` OR is `running` but whose container is absent in `docker ps` (ungraceful-shutdown recovery).
- `ExecStop=/usr/local/bin/drydock-stop-desks` — on shutdown, `ws stop <name>` every running drydock so the registry transitions to `suspended` authoritatively. systemd stops in reverse-start order, so this runs while `wsd` is still live.

`TimeoutStopSec=180` on the desks service covers heavy container teardowns.

## Install

From the drydock repo checkout on the Harbor:

```
bash /root/drydock/scripts/install-linux-services.sh
```

Idempotent. Creates `/root/.drydock/logs/` if missing. Enables both units for boot. Does **not** start them — do that explicitly the first time:

```
systemctl start drydock-wsd.service
systemctl start drydock-desks.service
```

On reboots after that, both come up automatically.

## Health

```
systemctl status drydock-wsd.service
ws daemon status
journalctl -u drydock-wsd.service -n 50
tail -f /root/.drydock/logs/wsd-systemd.log
tail -f /root/.drydock/logs/desks-resume.log
```

`ws daemon status` works under systemd. It treats "socket present + `wsd.health` responsive" as `running=true` regardless of pid-file presence — which matters because a systemd-managed daemon has no pid file at `~/.drydock/wsd.pid`.

## Gotchas known to surface

- **Ungraceful shutdown** (kernel panic, power loss) skips `ExecStop`. `drydock-resume-desks`' cross-check against live container state recovers: registry says `running` but container is gone → `ws stop` to mark suspended, then `ws create` to resume.
- **Daemon restart and container durability** — the socket bind-mount is a *directory bind*, not a file bind. A daemon restart that unlinks and recreates `wsd.sock` is transparent to running drydocks. Confirmed by calling `drydock-rpc wsd.whoami` from inside a drydock, `systemctl restart drydock-wsd`, retrying the same call without re-upping the container.
- **No macOS auto-resume.** macOS ships the launchd plist for `wsd` but no boot-sweep equivalent. Docker Desktop's VM suspend/resume covers the common laptop-close/open case; manual `ws create <name>` resumes anything that drops.
