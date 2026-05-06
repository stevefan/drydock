# Drydock smoke harness

End-to-end tests that exercise real containers, real capabilities, real
AWS. Run against a live Harbor — by default the one at `HARBOR_HOST`
(default: `root@drydock-hillsboro`).

Each scenario is a self-contained shell script under `scenarios/` that:

1. Claims a unique drydock name (`smoke-<scenario>-$$`).
2. Writes a throwaway project YAML on the Harbor.
3. Runs a lifecycle + assertions.
4. Tears down the drydock + any cloud resources it provisioned.

## Running

    # Single scenario
    scripts/smoke/run.sh storage-mounts

    # All
    scripts/smoke/run.sh

Scenarios exit non-zero on any failed assertion. The runner aggregates
results and fails CI-style (exit 1) if any scenario fails.

## Requirements

- SSH access to the Harbor as root (`~/.ssh/config` alias or the
  `HARBOR_HOST` env var).
- The Harbor already has `ws`, `drydock daemon`, and any capability-backend
  credentials wired up (drydock-runner AWS profile etc.).

## Why a separate harness

Unit tests (`tests/`) exercise pure logic against stubs. The smoke
harness exercises what that replaces — real docker, real iptables,
real STS. Bugs that only show under a real container start (sudoers
`env_keep`, FUSE device access, DNS resolution) are not catchable in
pytest; the harness is where they get caught.
