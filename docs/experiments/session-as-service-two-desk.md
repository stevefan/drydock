# Experiment: session-as-service — two-desk query probe

**Status:** proposed, not yet run. Standalone — assumes no prior conversation context.

## Why this experiment exists

The current multi-agent coordination pattern in this environment leans on shared substrates (files, queues, vaults). An alternative — **session-as-service** — skips the intermediate and lets agents coordinate by directly querying each other's Claude sessions. Agent A resumes agent B's session with a prompt, gets a reply, continues.

This is attractive because LLM sessions already collapse three things that are normally separate layers: memory (what the agent knows), channel (how it talks), and auth (identity / session-id). The collapse is free for LLM agents; it's not for traditional distributed systems.

Concept doc: `~/Notebooks/commonplace/slip-box/Substrate as precipitate - dialogues compose the graph.md`.

This experiment is the **minimum-viable mechanical test** of session-as-service: two desks, one queries the other, we measure what breaks.

## Hypothesis

A desk running `claude -p --resume <session-id> "<prompt>"` against a *peer desk's* session ID (reached over Tailscale) produces a coherent reply, measurably — and the failure modes (concurrency, drift, auth) are tractable enough to design against.

## Non-hypotheses (explicitly out of scope)

- **Substrate capture of the query.** This experiment tests only the live RPC; the precipitate ingestion into the hypergraph is tested separately (`~/Unified Workspaces/substrate/experiments/hypergraph/EXPERIMENT-dialogue-unification.md`).
- **V2 capability-brokered auth.** V2's capability broker isn't shipped; this experiment uses whatever auth is available today (Tailscale-level reachability + filesystem access to session files on the peer).
- **Session discovery / registry.** For v1 we pick two known session IDs by hand.
- **Multi-agent concurrent query.** Two agents querying ONE session. We note it as a failure mode; we don't exercise it.
- **Cross-host session sharing.** Both desks can be on the same host for this probe.

## Prerequisites

- Two drydock desks, reachable via Tailscale. `auction-crawl` (Hetzner) and any local desk, for example.
- A Claude session file on desk B with accumulated context about a topic desk A wants to ask about. Location: `~/.claude/projects/<project-slug>/<session-id>.jsonl` inside the desk.
- SSH or `drydock exec` access from desk A to desk B (drydock already provides Tailscale SSH).

## Protocol

### Probe 1: latency + baseline coherence

From desk A:

```bash
# Via drydock exec into desk B, or ssh over Tailscale, invoke claude --resume on desk B's session
ssh node@<desk-b-tailnet-hostname> \
  "cd <desk-b-project-path> && claude -p --resume <session-id> 'In one paragraph: what is the current state of <topic>, based on your memory of our conversation?'" \
  > probe_output.txt
```

Measure:

- **Wall-clock latency** (session resume + prompt + reply).
- **Reply coherence** — human eval: does it match what B actually knows? Or hallucinate? Or lose context?

Repeat 3–5 times with different topics.

### Probe 2: mutation behavior

Does resuming + prompting MODIFY session B's history? Check:

1. Note desk B's session jsonl line count before the probe.
2. Run probe from desk A.
3. Check line count after.

If the session gets appended to, every probe from desk A leaves a trace in desk B's history. For read-only queries, this is wrong. Probe results determine whether we need a `--detached` / `--dry-run` mode.

### Probe 3: concurrency race

1. From two separate shells on desk A (or two desks), fire `claude -p --resume <SAME session-id>` simultaneously.
2. Observe: do both succeed? Does one fail? Do replies diverge? Does session history fork?

Expected: this breaks or serializes. We want to know *how* it breaks so the session-query primitive can fail safely.

### Probe 4: drift window

1. Identify a session B whose latest activity is ≥ 1 week old.
2. Query desk B's session about something that has happened since (a recent commit, a recent decision).
3. Observe: does B confidently assert stale facts? Hedge? Know it's out of date?

Calibrates how stale is too stale before the query surface becomes misleading.

### Probe 5: auth boundary probe (light)

Today's reality: if desk A can `ssh node@desk-b`, it can `claude -p --resume` any session file it can read. That's filesystem-level auth, not policy. Confirm:

1. From a desk WITHOUT Tailscale access to desk B: attempt the probe. Expect failure.
2. From a desk WITH Tailscale access but without the session ID: can it enumerate? (Yes, if it can `ls` desk B's `.claude/projects/`.) Note this as a v1 trust gap that V2 capability-broker closes.

## Success criteria

- **Minimum:** probes 1 and 2 complete; we have measured latency + know whether history mutates.
- **Target:** probes 1–4 complete; we have a written characterization of each failure mode.
- **Stretch:** a short write-up (in `agent-output/drydock/`) summarizing what we learned, feeding into the V2 capability-broker design for a `SESSION_QUERY` capability type.

## What this experiment earns

- **Evidence (or counter-evidence) for session-as-service.** If probe 1 produces coherent replies with acceptable latency and probe 2 doesn't mutate on simple reads, the primitive is viable. If every probe degrades or mutates, the primitive isn't ready and substrate shoulders more coordination weight.
- **A concrete failure-mode inventory** for V2 capability design. The `scope` dict for a future `SESSION_QUERY` capability should encode what the probes surfaced (read-only vs. mutating, concurrency serialization, drift-threshold hints, discovery surface).
- **A real two-desk test of drydock V1's Tailscale reachability** for something beyond `drydock attach`. Bonus.

## Open design questions the results should inform

1. Should a `SESSION_QUERY` capability differentiate read-only vs. mutating queries? (Probe 2 answers this.)
2. Should the daemon serialize queries against the same session? (Probe 3 answers this.)
3. Should the daemon decorate queries with a "last session activity:" header to let the occupant assess drift? (Probe 4 informs this.)
4. Does session discovery need a daemon-side registry? Or is "you bring your own session-id" sufficient for V2? (Probe 5 frames this.)

## Failure modes to watch for

- **Session lock file / IPC conflict.** Claude Code may hold a lock on active sessions. Resuming a session that's in use by a live Claude process may fail or interfere.
- **Filesystem access not granted to peer user.** drydock exec runs as the desk's container user; session files may have restrictive permissions.
- **Model mismatch.** Desk A queries at a moment when desk B's session was using a different model. Output style differs. Not a bug, but worth noting.
- **Drift masquerading as wrong answer.** B's session "knows" about a situation that has changed. Reply is coherent but wrong. The worst failure mode — tests should explicitly include a post-session event.

## Estimated scope

Half a day to a full day once both desks are up. Most of the time is in evaluation (reading replies, comparing to ground truth) not in running the probes.

## Follow-ups (not this experiment)

- Add session discovery / registry to drydock V2 daemon as a reserved future capability.
- Wrap the probe in a small `drydock session-query <peer-desk> <session-id> "<prompt>"` CLI once the primitive is validated.
- Combine with substrate ingestion: the probe's reply AND the queried session both ingest into the hypergraph, giving a compounding knowledge surface.
- Multi-desk orchestration patterns: agent-of-agents, where one desk's occupant routes sub-questions to peer desks.

## References

- `~/Notebooks/commonplace/slip-box/Substrate as precipitate - dialogues compose the graph.md` — the conceptual frame
- `~/Unified Workspaces/substrate/experiments/hypergraph/EXPERIMENT-dialogue-unification.md` — sibling experiment on the substrate side (precipitate, not live-query)
- `docs/design/capability-broker.md` — where a future `SESSION_QUERY` capability would live
- `docs/design/tailnet-identity.md` — the peer-reachability primitive this experiment rides on
