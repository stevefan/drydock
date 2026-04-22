# Deskwatch — workload health layer

**Status:** design sketch. Implementation targeted for next session.

## Problem

Drydock tells you the container is running. It doesn't tell you the workload is healthy.

Today: `ws list` shows `running`, `ws status` shows every infra probe green. Meanwhile the daily-crawl cron has been crashing for four days, the data file hasn't moved, no alert has fired. You only find out by looking.

Drydock's scope ends at "put the workload in a correctly-configured box." What's missing is a thin layer above that says "the workload did its job today."

## Scope

Deskwatch is a **drydock-native health-check layer** covering three kinds of signal:

1. **Scheduled-job outcomes.** Desks with `deploy/schedule.yaml` have jobs that should run on cron/launchd/systemd cadence. Deskwatch tracks: did the last expected run happen, what was its exit code, was its output produced?
2. **Output freshness.** Desks that produce artifacts (DB files, S3 objects, markdown alerts) expect those artifacts to move on some cadence. Deskwatch tracks file mtimes and age-vs-expected.
3. **Smoke-probe results.** A desk can declare a lightweight "am I alive" probe (a one-shot command that exits 0 when healthy). Deskwatch runs it periodically and records outcomes.

Deskwatch **does not** try to repair unhealthy desks. It only observes and reports. Repair stays a human (or smart-operator) decision.

## Surface

### Project YAML

```yaml
# ~/.drydock/projects/auction-crawl.yaml
deskwatch:
  jobs:
    # One entry per schedule.yaml job. Deskwatch reads schedule.yaml to get
    # the cadence; this block adds health expectations on top.
    - name: daily-crawl
      expect_success_within: 25h   # no success in this window → unhealthy
    - name: operator-morning
      expect_success_within: 25h
  outputs:
    - path: /workspace/data/auction_crawl.db
      max_age: 25h                 # mtime > this → unhealthy
    - path: /workspace/data/auction-alerts.md
      max_age: 25h
      may_be_empty: true           # don't require non-zero size
  probes:
    - name: db-readable
      cmd: "test -r /workspace/data/auction_crawl.db"
      interval: 1h
```

All three sections optional. A desk with no `deskwatch:` block gets no workload-level checks (back-compat).

### CLI

```bash
ws deskwatch <name>          # one desk's status
ws deskwatch                 # all desks, table form
ws deskwatch <name> --json   # for scripting / piping to alert sinks
```

Output (human):

```
auction-crawl
  daily-crawl:    last run 2026-04-21 13:00 UTC → exit 0 (age: 6h, within 25h ✓)
  operator-morning: last run 2026-04-21 01:37 UTC → exit 0 (age: 18h, within 25h ✓)
  outputs:
    auction_crawl.db   2026-04-17 13:03 UTC (age: 4d 2h, EXCEEDS 25h ✗)
    auction-alerts.md  missing ✗
  probes:
    db-readable      last: 2026-04-21 15:00 UTC → ok

overall: UNHEALTHY (2 violations)
```

Exit code: 0 if healthy, 1 if unhealthy (enables `ws deskwatch foo && echo ok || alert`).

### State

Deskwatch keeps its own SQLite table in the existing registry DB:

```sql
CREATE TABLE deskwatch_events (
  desk_id       TEXT NOT NULL,
  kind          TEXT NOT NULL,      -- 'job_run', 'probe_result', 'output_check'
  name          TEXT NOT NULL,      -- job name / probe name / output path
  timestamp     TEXT NOT NULL,
  status        TEXT NOT NULL,      -- 'ok', 'failed', 'missing'
  detail        TEXT,               -- exit code, stderr tail, file size, etc.
  PRIMARY KEY (desk_id, kind, name, timestamp)
);
```

Job-run events are populated by wrapping the scheduler's existing cron/launchd lines: `ws schedule sync` already writes `0 13 * * * ws exec ...` entries; deskwatch changes the wrapper to `ws exec ...; ws deskwatch-record <desk> <job> $?`. Zero runtime cost when healthy, a row when anything fires.

Output-freshness and probes run when `ws deskwatch` is invoked (lazy probing), OR when a periodic `ws deskwatch --scan` is scheduled (a systemd timer or cron). Lazy is cheaper; scan is needed for proactive alerting.

## How alerts reach the human

Orthogonal concern. Three layers:

1. **`ws deskwatch` exit code** — for cron/systemd wrappers to alert themselves.
2. **`ws deskwatch --json`** — structured output for piping to whatever (Slack webhook, email, claude prompt).
3. **A drydock employee pattern (future).** A long-running "watcher" desk whose job is to `ws deskwatch --json` every 15min, decide whether to escalate, and post to wherever Steven reads. That's the natural home for judgment ("crawl failed once — not yet worth paging; three times — page").

Ship layers 1 + 2 first. Layer 3 piggybacks on the existing drydock-employee pattern (`project_drydock_employee_pattern.md` in memory) and doesn't need deskwatch-specific plumbing.

## Why this shape

- **Observes, doesn't repair.** Healing is a separate concern; mixing it here would couple a health signal to a remediation policy, and the right remediation varies per workload.
- **Declarative, per-project.** Expectations live in the project YAML next to the schedule/overlay, so they version with the workload. Drydock doesn't guess; the workload owner declares.
- **Uses existing state store.** Piggybacks on the registry DB; no new service, no external dependency.
- **Symmetric across Harbors.** Peer-Harbor design (see `project_peer_harbors_decision.md`): each Harbor runs its own deskwatch on its own desks. No cross-Harbor aggregation until federation lands.

## What NOT to build (scope guard)

- Alerting infrastructure (email, Slack, PagerDuty integration) — out of scope. The employee pattern handles that.
- Auto-remediation (restart, rebuild, rotate) — out of scope.
- Historical dashboards / metrics / timeseries — out of scope; the SQLite table is observational, not a monitoring backend.
- Cross-Harbor health views — out of scope until federation.

## Ship plan

1. Schema + `ws deskwatch-record` (internal helper invoked by scheduled wrappers).
2. `ws deskwatch` read-side (jobs from events table, outputs via `docker exec stat`, probes run at request time).
3. Project YAML parsing for the `deskwatch:` block.
4. Scheduler wrapper update — existing `ws schedule sync` appends the record call.
5. Smoke scenario: a desk with a guaranteed-failing job → `ws deskwatch` reports UNHEALTHY with exit 1.

Rough size: 300-500 LOC + tests. One session.
