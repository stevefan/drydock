# Agent Workspace Orchestration Spec v1

## 1. Purpose

Provide a repeatable system for creating, managing, and steering agent workspaces across projects and branches, with good support for:
- persistent development environments
- branch-isolated task work
- mobile session steering
- controlled resource mounting
- network attachment
- cloud and infra actions
- transcript and conversation persistence
- multiple concurrent workspaces

The system should make workspace the main abstraction, not container or terminal session.

---

## 2. Core design thesis

A workspace is a named, durable task context composed of:
- a repo target
- a git worktree
- a runtime container
- a conversation/session record
- attached resources and permissions
- one or more control surfaces

Containers are disposable.
Workspaces are durable.
Conversation state and task state belong to the workspace.

---

## 3. Non-goals

Version 1 should not try to be:
- a general CI/CD platform
- a full Kubernetes replacement
- a multi-tenant enterprise scheduler
- a generalized agent protocol standard
- a perfect mobile IDE
- a replacement for GitHub, GitLab, or source control hosting

It is a personal or small-team agent workspace control plane.

---

## 4. Primary use cases

### 4.1 Persistent dev workspace
A long-lived environment for ongoing work in a repo.

### 4.2 Forked branch workspace
Create an isolated task branch from an existing workspace and attach a new agent session.

### 4.3 Infra workspace
Launch a workspace with cloud tools, credentials, and network access for infra tasks.

### 4.4 Mobile check-in
Inspect current workspaces from phone, view summary, attach to session, nudge agent, or fork a new task.

### 4.5 Conversation continuity
Retain a usable record of the agent's task context, summaries, key decisions, and state transitions.

---

## 5. User-facing concepts

### 5.1 Workspace
The top-level unit of work.

Properties:
- unique id
- human-readable name
- project/repo binding
- branch/worktree binding
- runtime profile
- permission profile
- state directory
- transcript history
- attached interfaces

### 5.2 Project
A source repository or codebase target.

Properties:
- canonical repo path
- remote URL
- default branch
- build/runtime template
- workspace defaults

### 5.3 Worktree
A checked-out filesystem instance of a repo branch or commit, created via git worktree.

### 5.4 Runtime
The execution environment for the workspace, usually a container.

### 5.5 Session
An interactive agent or terminal process associated with a workspace.

Examples:
- Claude Code session
- tmux session
- shell session
- long-running background command

### 5.6 Profile
A reusable configuration bundle.

Examples:
- runtime profile
- permission profile
- network profile
- mount profile

### 5.7 Control surface
A means of interacting with a workspace.

Examples:
- SSH
- tmux attach
- Claude Code remote control
- VibeTunnel/browser terminal
- web/mobile dashboard

---

## 6. System architecture

### 6.1 Layers

**Layer A: substrate**

Persistent machine or VM providing:
- storage
- container runtime
- networking
- identity
- secret sources

**Layer B: workspace control plane**

Responsible for:
- workspace registry
- lifecycle actions
- state model
- policy enforcement
- status and summary views

**Layer C: runtime execution**

Responsible for:
- container launch
- resource mounts
- env injection
- process supervision
- network attachment

**Layer D: interaction surfaces**

Responsible for:
- session attach
- browser/mobile views
- summaries
- quick actions

---

## 7. Functional requirements

### 7.1 Workspace lifecycle

System must support:
- create workspace
- fork workspace
- start workspace
- stop workspace
- suspend workspace
- resume workspace
- destroy workspace
- archive workspace
- list workspaces
- inspect workspace
- attach to workspace

### 7.2 Repo integration

System must support:
- register project repo
- create git worktree for workspace
- bind one workspace to one worktree
- create branch on workspace creation if needed
- optionally start from existing branch
- optionally start detached from commit or PR head

### 7.3 Runtime integration

System must support:
- start workspace in container
- mount worktree into runtime
- mount persistent state directories
- attach runtime to one or more networks
- select image/template per workspace
- inject environment and secrets according to profile

### 7.4 Conversation/session management

System must support:
- create agent session for workspace
- record session metadata
- store transcript artifacts
- store agent summaries/checkpoints
- allow session re-attach
- allow a fresh session to inherit selected workspace context

### 7.5 Mobile/remote control

System should support:
- list active workspaces
- see health/status
- see last summary
- attach to terminal
- attach to agent conversation
- restart session
- fork from existing workspace

### 7.6 Resource attachment

System must support attaching:
- filesystem mounts
- network memberships
- cloud credentials
- service tokens
- local tools or MCP endpoints
- artifact storage

---

## 8. Non-functional requirements

### 8.1 Reliability
A crashed container must not destroy the workspace record or transcript history.

### 8.2 Portability
System should work on a single Linux host first. Cloud and multi-host support can come later.

### 8.3 Low friction
Creating a new task workspace should take one command.

### 8.4 Auditability
Important workspace actions should be logged.

### 8.5 Security
Permissions should attach to workspace profiles, not only to machine identity.

### 8.6 Recoverability
Workspace state must be reconstructible from registry plus state directory plus worktree.

---

## 9. Workspace state model

Each workspace has a lifecycle state:
- defined
- provisioning
- ready
- running
- idle
- suspended
- error
- archived
- destroyed

Suggested semantics:
- **defined**: spec exists, not provisioned
- **provisioning**: worktree/runtime/session setup in progress
- **ready**: provisioned, no active interactive session
- **running**: active runtime and session
- **idle**: runtime exists, no recent activity
- **suspended**: runtime stopped, state preserved
- **error**: provisioning or runtime failure
- **archived**: preserved for history, not intended for active use
- **destroyed**: removed from active system

---

## 10. Data model

### 10.1 Workspace record

```yaml
apiVersion: agentops/v1
kind: Workspace
metadata:
  id: ws_payments_001
  name: payments-refactor
  createdAt: 2026-04-10T10:00:00-07:00
  labels:
    project: app
    purpose: feature
spec:
  project: app
  repoPath: /srv/code/app
  worktree:
    path: /srv/worktrees/app-payments-refactor
    branch: steven/payments-refactor
    baseRef: origin/main
    createBranch: true
  runtime:
    provider: docker
    image: ghcr.io/org/app-dev:2026-04
    workingDir: /workspace
    command: ["bash"]
  mounts:
    - type: bind
      source: /srv/worktrees/app-payments-refactor
      target: /workspace
      mode: rw
    - type: bind
      source: /srv/agent-state/ws_payments_001
      target: /agent-state
      mode: rw
  networks:
    - tailscale
    - internal-dev
  permissions:
    profile: dev-readwrite
  sessions:
    agent:
      type: claude-code
      enabled: true
    terminal:
      type: tmux
      sessionName: ws-payments-001
  surfaces:
    ssh: true
    vibetunnel: true
    remoteControl: true
status:
  phase: ready
  runtimeId: container_abc123
  lastSummary: "Workspace created from origin/main. No changes yet."
  lastActivityAt: 2026-04-10T10:02:13-07:00
```

---

## 11. Directory layout

Recommended host layout:

```
/srv/agent-ops/          # orchestrator code/config
/srv/code/               # canonical repos
/srv/worktrees/          # active workspace worktrees
/srv/agent-state/        # per-workspace durable state
/srv/cache/              # shared caches
/srv/secrets/            # controlled secret material or references
```

Per-workspace state directory:

```
/srv/agent-state/ws_payments_001/
  workspace.yaml
  summaries/
    0001.md
    0002.md
  transcripts/
    session-2026-04-10-1002.jsonl
  logs/
    runtime.log
    events.log
  artifacts/
  checkpoints/
```

---

## 12. Git worktree rules

### 12.1 Worktree invariants
- one workspace maps to one worktree
- one worktree maps to one branch or commit target
- active writable workspaces should not share the same branch
- canonical repo remains separate from active worktrees

### 12.2 Recommended strategy
- keep canonical repo in `/srv/code/<project>`
- create workspace worktrees in `/srv/worktrees/<project>-<workspace-name>`
- default to creating a branch on workspace create
- allow forking from existing workspace by branching from its current HEAD

### 12.3 Fork semantics

Forking a workspace creates:
- new workspace id
- new worktree path
- new branch
- copied or referenced task brief
- fresh transcript lineage with parent reference

---

## 13. Runtime spec

### 13.1 Runtime abstraction

Version 1 runtime types:
- docker
- optionally podman later

### 13.2 Runtime requirements

Each runtime must support:
- bind mounts
- named network attachment
- env injection
- restart policy
- identifiable runtime id
- log capture

### 13.3 Runtime templates

Runtime templates define:
- image
- shell
- toolchain
- startup hooks
- healthcheck command
- optional MCP or tool wiring

Example:

```yaml
runtimeTemplates:
  python-node-dev:
    image: ghcr.io/org/python-node-dev:latest
    workingDir: /workspace
    env:
      PIP_CACHE_DIR: /cache/pip
      NPM_CONFIG_CACHE: /cache/npm
    mounts:
      - /srv/cache:/cache
```

---

## 14. Permission model

### 14.1 Principle
Permissions belong to the workspace profile.

### 14.2 Permission profile examples
- readonly-code
- dev-readwrite
- staging-readonly
- infra-admin
- cloud-disabled

### 14.3 Controlled resources

Permission profile may govern:
- writable repo access
- AWS credential access
- Kubernetes context access
- secret injection
- network attachment
- outbound internet access
- local host socket access

### 14.4 Safety default

Default new workspaces should launch with:
- repo write enabled
- cloud disabled unless explicitly requested
- minimal secrets
- named network only

---

## 15. Session model

### 15.1 Session types
- agent
- terminal
- background-job

### 15.2 Agent session record

Each agent session should store:
- session id
- workspace id
- agent type
- started at
- ended at
- transcript path
- summary path
- parent session id if forked from another

### 15.3 Summary checkpointing

System should support:
- manual summary write
- auto-summary on suspend
- auto-summary on fork
- auto-summary on error

Summary should include:
- task
- current branch
- notable files changed
- current blockers
- suggested next action

---

## 16. Control surface spec

### 16.1 SSH
Ground-truth access method.

Required:
- deterministic host path
- deterministic tmux session naming
- attach command generation

### 16.2 tmux
Canonical terminal multiplexer.

Convention:
- one tmux session per workspace
- standard windows: agent, shell, logs

### 16.3 Browser terminal
Optional layer such as VibeTunnel.

Should support:
- attach to workspace terminal
- read logs
- quick command input

### 16.4 Agent remote control
If supported by agent tool, may attach a synchronized conversation UI to an active session.

Should be treated as:
- optional overlay
- not sole source of truth

### 16.5 Mobile dashboard
Version 1 can be minimal.

Must show:
- name
- status
- branch
- project
- last summary
- quick attach links

---

## 17. CLI spec

A small CLI is the right front door.

### 17.1 Commands

```
ws create <project> <name> --from <ref> --profile <profile>
ws fork <source> <name> --profile <profile>
ws list
ws inspect <name>
ws attach <name>
ws summary <name>
ws stop <name>
ws resume <name>
ws archive <name>
ws destroy <name>
```

---

## 18. CLI behavior contract

### 18.1 ws create
Must:
- validate project exists
- create worktree
- allocate state dir
- start container
- create tmux session
- optionally launch agent session
- register workspace

### 18.2 ws fork
Must:
- capture current source workspace HEAD
- create new branch/worktree
- create child workspace record
- inherit allowed context
- not copy raw transient shell state

### 18.3 ws attach
Should:
- prefer tmux attach over raw shell
- show alternatives if browser terminal exists
- work over SSH

### 18.4 ws summary
Should:
- show latest checkpoint summary
- show branch
- show changed files
- show runtime status

---

## 19. Inheritance model for forks

When forking a workspace, inherit:
- project binding
- runtime template
- mount profile
- network profile
- selected task brief
- selected summaries

Do not inherit by default:
- full transcript history
- stale terminal history
- dead jobs
- unbounded scratch notes
- unrelated environment overrides

A fork should feel like a clean child task, not a messy clone.

---

## 20. Logging and event model

System should emit structured events:
- workspace.created
- workspace.started
- workspace.stopped
- workspace.forked
- workspace.suspended
- workspace.archived
- runtime.failed
- session.started
- session.ended
- summary.created

Suggested event record:

```json
{
  "timestamp": "2026-04-10T10:03:00-07:00",
  "event": "workspace.forked",
  "workspace_id": "ws_payments_fix_tax",
  "parent_workspace_id": "ws_payments_001",
  "branch": "steven/payments-fix-tax"
}
```

---

## 21. Failure handling

### 21.1 Container crash
Mark runtime unhealthy but preserve workspace record and state directory.

### 21.2 Worktree creation failure
Abort create and clean partial state.

### 21.3 Session launch failure
Workspace can still enter ready with no active agent session.

### 21.4 Network attachment failure
Fail provisioning unless profile allows degraded start.

### 21.5 Manual filesystem drift
Provide repair command: `ws repair <workspace>`

---

## 22. Security model

### 22.1 Assumptions
Version 1 is primarily single-user or trusted small-team.

### 22.2 Security controls
Should include:
- per-workspace secret injection
- network profile restrictions
- no blanket host socket mount by default
- minimal cloud credential scope
- audit log of privileged actions

### 22.3 Dangerous capabilities
Require explicit enablement:
- docker socket access
- host root filesystem mount
- production cloud credentials
- destructive infra commands
- public ingress exposure

---

## 23. API surface

Core operations:
- CreateWorkspace(spec)
- ForkWorkspace(parentId, childSpec)
- StartWorkspace(id)
- StopWorkspace(id)
- SuspendWorkspace(id)
- DestroyWorkspace(id)
- GetWorkspace(id)
- ListWorkspaces(filter)
- WriteSummary(id, summary)
- AttachSurface(id, surfaceType)

This lets you later add REST API, TUI, web dashboard, or mobile app without changing core logic.

---

## 24. Minimal v1 implementation plan

### Phase 1
Get a useful CLI-only system working.

Includes:
- workspace registry in YAML or SQLite
- git worktree lifecycle
- docker runtime
- tmux session management
- state directory creation
- SSH attach command
- summary files

### Phase 2
Add browser/mobile comfort.

Includes:
- workspace list page
- attach links
- status summaries
- VibeTunnel integration

### Phase 3
Add richer policy and orchestration.

Includes:
- permission profiles
- network profiles
- cloud credentials
- better session lineage
- agent-specific adapters

---

## 25. Open design questions

### 25.1 Registry backend
Start with YAML files or SQLite. SQLite is probably the better default.

### 25.2 Transcript format
Options: raw JSONL, markdown summaries + raw transcript refs, or hybrid.

### 25.3 Background jobs
Do they belong as sessions inside a workspace, or separate job objects?

### 25.4 Multi-host support
Will a workspace always live on one host, or do you want host scheduling later?

### 25.5 Secret source
Will secrets come from: local files, 1Password/Bitwarden bridge, cloud secret manager, or environment injection only?

---

## 26. Recommended v1 boundaries

**In scope:**
- one host
- Docker
- git worktrees
- tmux
- SSH
- state directories
- workspace registry
- branch forking
- summary/checkpoint files
- optional VibeTunnel integration

**Out of scope:**
- Kubernetes
- distributed scheduling
- heavy RBAC
- multi-user tenancy
- fancy autonomous task planner
- generalized cloud resource graph

---

## 27. Top-level summary

Agent Workspace Orchestration is a control plane for creating named, branch-isolated, container-backed workspaces with persistent task state, conversation history, and multiple remote control surfaces. A workspace is the durable unit of work. Git worktrees provide source isolation, containers provide runtime isolation, and workspace state directories provide continuity across sessions and failures.

---

## 28. Suggested next artifact

1. workspace manifest schema
2. CLI command contract
3. directory/state layout
4. lifecycle state machine
5. MVP implementation plan

The best immediate next step is the workspace manifest schema plus lifecycle state machine, because those force the abstractions to become concrete.
