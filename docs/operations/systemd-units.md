# Systemd units (Linux Harbors)

`scripts/install-linux-services.sh` installs two units and two helper scripts. Together they make `systemctl restart drydock-daemon` transparent to running drydocks and `reboot` self-heal for every drydock that was running.

Design rationale in [../design/persistence.md](../design/persistence.md). macOS equivalent is `base/com.drydock.plist` (launchd user agent).

## What installs where

| Source | Target | Mode |
|---|---|---|
| `base/drydock.service` | `/etc/systemd/system/drydock.service` | 0644 |
| `base/drydock-desks.service` | `/etc/systemd/system/drydock-desks.service` | 0644 |
| `scripts/drydock-resume-desks` | `/usr/local/bin/drydock-resume-desks` | 0755 |
| `scripts/drydock-stop-desks` | `/usr/local/bin/drydock-stop-desks` | 0755 |
| `scripts/drydock-rpc` | `/usr/local/bin/drydock-rpc` **and** `/root/.drydock/bin/drydock-rpc` | 0755 |

`drydock host init` also deploys `drydock-rpc` to `~/.drydock/bin/` — the overlay bind-mount source path — so creating a drydock picks it up without running the install script.

## Units

**`drydock.service`** — long-running daemon, `Restart=on-failure`. Runs as root. Binds `~/.drydock/run/daemon.sock` and chmods it `0o666` so container uid 1000 can connect (bearer token is the real security gate — see [../design/in-desk-rpc.md](../design/in-desk-rpc.md)). Logs append to `/root/.drydock/logs/daemon-systemd.log`.

**`drydock-desks.service`** — `Type=oneshot`, `RemainAfterExit=yes`, `After=drydock.service`, `Requires=drydock.service`. Two hooks:
- `ExecStart=/usr/local/bin/drydock-resume-desks` — poll `drydock daemon status`, then `drydock create <name>` for every drydock whose registry state is `suspended` OR is `running` but whose container is absent in `docker ps` (ungraceful-shutdown recovery).
- `ExecStop=/usr/local/bin/drydock-stop-desks` — on shutdown, `drydock stop <name>` every running drydock so the registry transitions to `suspended` authoritatively. systemd stops in reverse-start order, so this runs while `drydock daemon` is still live.

`TimeoutStopSec=180` on the desks service covers heavy container teardowns.

## Install

From the drydock repo checkout on the Harbor:

```
bash /root/drydock/scripts/install-linux-services.sh
```

Idempotent. Creates `/root/.drydock/logs/` if missing. Enables both units for boot. Does **not** start them — do that explicitly the first time:

```
systemctl start drydock.service
systemctl start drydock-desks.service
```

On reboots after that, both come up automatically.

## Health

```
systemctl status drydock.service
drydock daemon status
journalctl -u drydock.service -n 50
tail -f /root/.drydock/logs/daemon-systemd.log
tail -f /root/.drydock/logs/desks-resume.log
```

`drydock daemon status` works under systemd. It treats "socket present + `daemon.health` responsive" as `running=true` regardless of pid-file presence — which matters because a systemd-managed daemon has no pid file at `~/.drydock/daemon.pid`.

## Gotchas known to surface

- **Ungraceful shutdown** (kernel panic, power loss) skips `ExecStop`. `drydock-resume-desks`' cross-check against live container state recovers: registry says `running` but container is gone → `drydock stop` to mark suspended, then `drydock create` to resume.
- **Daemon restart and container durability** — the socket bind-mount is a *directory bind*, not a file bind. A daemon restart that unlinks and recreates `daemon.sock` is transparent to running drydocks. Confirmed by calling `drydock-rpc daemon.whoami` from inside a drydock, `systemctl restart drydock-daemon`, retrying the same call without re-upping the container.
- **No macOS auto-resume.** macOS ships the launchd plist for `drydock daemon` but no boot-sweep equivalent. Docker Desktop's VM suspend/resume covers the common laptop-close/open case; manual `drydock create <name>` resumes anything that drops.
