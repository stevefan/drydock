# Notebooks Sync — headless Syncthing pattern

Replaces the Mac-only `source=${HOME}/Notebooks` bind-mount with a
per-Harbor Syncthing desk that hosts the canonical vault. Other desks
on that Harbor mount the synced data as a named volume; the peer-Harbor
model stays intact (each Harbor has its own notebooks desk, its own
Syncthing node, paired with the Mac where the vault originates).

## Shape

```
┌─ Mac ──────────────┐                    ┌─ Hetzner Harbor ─────────────────┐
│  ~/Notebooks       │                    │  notebooks desk                  │
│  Syncthing (brew)  │──── tailnet ─────→ │  Syncthing → /vault              │
│  device: LE66…     │                    │  device: IDFG…                   │
└────────────────────┘                    │                                  │
                                          │  sibling desks (substrate, …)    │
                                          │  mount notebooks-vault readonly  │
                                          └──────────────────────────────────┘
```

Each Harbor can run its own notebooks desk paired with the same Mac
Syncthing node (or any peer node you designate as canonical). No
cross-Harbor dependency — federation is not required.

## Standing up a notebooks desk

```bash
# On the Harbor (repo is stubbed under /root/src/notebooks):
git -C /root/src/notebooks init
cp scripts/notebooks/{Dockerfile,start-syncthing.sh,devcontainer.json} \
   /root/src/notebooks/.devcontainer/drydock/
cp scripts/notebooks/project.yaml ~/.drydock/projects/notebooks.yaml
git -C /root/src/notebooks add -A && git -C /root/src/notebooks commit -m init

ws create notebooks
```

First start generates the Syncthing device ID:

```bash
ws exec notebooks -- syncthing --home=/var/lib/syncthing device-id
#   IDFGAVP-A33YIMB-M5HAPYJ-SQ2QBO7-7FIS5EO-LUYCXM7-FXHVV7F-CGNB7A5
```

## Pairing with your Mac

**Do not** sync the whole `~/Notebooks` directory indiscriminately —
it contains vault-scoped subfolders (notably `asi/`, marked
"intentionally siloed" in your workspace CLAUDE.md) that belong to
separate trust domains. Pick individual vaults to expose per Harbor.

### 1. Pick the vaults this Harbor gets

Each vault shares independently. Decide which subset belongs on this
Harbor. Example for substrate-adjacent use:

- `~/Notebooks/commonplace`  — general thinking (substrate reads)
- `~/Notebooks/lab`          — microfoundry knowledge
- `~/Notebooks/inbox`        — capture buffer

Not shared:

- `~/Notebooks/asi`          — collaborator silo
- anything else you want kept Mac-local

### 2. Configure the Mac Syncthing node

Install + start:

```bash
brew install syncthing
brew services start syncthing
syncthing device-id   # LE666NX-5EPMWQ5-6JBRFG3-NCRNL6A-...
```

Open http://localhost:8384 in your browser.

For each vault you picked in step 1:
1. "Add folder"
2. Folder ID: `notebooks-<vault>` (e.g. `notebooks-commonplace`)
3. Folder path: `/Users/<you>/Notebooks/<vault>`
4. Share with: the Hetzner device ID (you'll add the device first —
   "Add Remote Device", paste Hetzner's device ID, give it a friendly
   name, check "Introducer: no")

### 3. Accept on the Hetzner side

`tailscale serve` already exposes the Hetzner desk's Syncthing UI at
port 8384. From your Mac (on the tailnet):

```bash
open http://notebooks:8384
```

For each folder the Mac offered:
1. You'll see a "Pending" notification
2. Accept, set the folder path to `/vault/<vault>` (e.g.
   `/vault/commonplace`). Syncthing creates subdirs automatically.
3. Check the shared-with box.

Syncthing completes the initial sync (one-time, proportional to vault
size — ~300MB for lab, ~200MB for commonplace based on your setup).

### 4. Consume from sibling desks

Add to the consuming desk's project YAML:

```yaml
extra_mounts:
  - "source=notebooks-vault,target=/workspace/Notebooks,type=volume,readonly"
```

Then `ws project reload <desk>` + `ws stop && ws create` to apply.
Every vault shared ends up at `/workspace/Notebooks/<vault>` inside
the consuming desk.

## Operational notes

- **Desk-level firewall allows the Syncthing infrastructure** (relay
  + discovery + apt repo). See `project.yaml`.
- **Syncthing state lives on a named volume** (`notebooks-syncthing-state`)
  so `ws stop && ws create` doesn't re-pair. To force re-pairing:
  `docker volume rm notebooks-syncthing-state`, then `ws create
  --force`.
- **The `notebooks-vault` named volume** is what sibling desks mount.
  Don't bind-mount it with `:rw` unless you want writes flowing
  back through Syncthing — for a notes-consumer desk, `readonly` is
  the right default.
- **Deskwatch** watches the Syncthing process (`pgrep -x syncthing`)
  and the config file. Extend with a probe that reads
  `/rest/system/status` if you want freshness signal.

## Limitations of V1

- Vault selection is manual in both GUIs; no declarative project-YAML
  surface for "share these vaults." If this stays a per-Harbor-one-off,
  that's fine; if you want it reproducible, add a follow-up to express
  the share set as YAML that `ws create notebooks` applies via
  Syncthing's REST API.
- Pairing is interactive (click accept in both GUIs). Automating this
  requires passing Steven's explicit share list, which is the
  boundary-sensitive decision the silo rule protects.
- No `STORAGE_MOUNT`-style cross-Harbor lease for notebooks vaults
  yet. Peer Harbors each pair with Mac independently. Works fine
  while there are 1-2 Harbors; revisit when there's actual islanding
  pain.
