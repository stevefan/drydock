"""ws sync — pull source-repo changes into a desk's worktree.

The worktree was cloned from ``ws.repo_path`` at create time and is
never updated afterward — so changes the user makes in the source repo
(fixing a bug, updating devcontainer.json, bumping a pinned version)
don't reach the desk until a destroy + recreate. That's heavy enough
that people start editing files directly in the worktree on the
Harbor, which drifts the worktree from the source repo and breaks the
next rebuild.

``ws sync <name>`` adds a ``source`` remote pointing at ``ws.repo_path``
(if not already present), fetches it, and fast-forward-merges the
source repo's current branch into the desk's ``ws/<name>`` branch.

Abort-loudly on:
- worktree has uncommitted changes (user work would be clobbered)
- non-ff (branches have diverged; ask user to rebase or merge manually)

The running container does NOT pick up worktree changes that live
inside the container fs (installed packages, postCreate artifacts).
For those, a `ws stop && ws create` rebuild is still needed; ``ws
sync`` just lines up the source-of-truth on disk so the rebuild
produces correct output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from drydock.core import WsError


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


@click.command()
@click.argument("name")
@click.option(
    "--source-branch",
    default=None,
    help="Source repo branch to sync from (defaults to source's current HEAD branch).",
)
@click.pass_context
def sync(ctx, name, source_branch):
    """Fast-forward the desk's worktree from its source repo."""
    out = ctx.obj["output"]
    registry = ctx.obj["registry"]

    ws = registry.get_workspace(name)
    if ws is None:
        out.error(WsError(f"Drydock '{name}' not found",
                          fix="Check `ws list` for the name",
                          code="desk_not_found"))
        return

    worktree = Path(ws.worktree_path)
    source = Path(ws.repo_path)

    if not worktree.exists():
        out.error(WsError(f"Worktree missing: {worktree}",
                          fix=f"Run `ws create {name}` to recreate it",
                          code="worktree_missing"))
        return
    if not (source / ".git").exists():
        out.error(WsError(f"Source repo missing: {source}",
                          fix="Check repo_path in the desk's project YAML",
                          code="source_missing"))
        return

    # Abort if the worktree has uncommitted changes; syncing would
    # either block on merge or silently reorder work-in-progress.
    status = _git(worktree, "status", "--porcelain")
    if status.stdout.strip():
        out.error(WsError(
            f"Worktree has uncommitted changes: {worktree}",
            fix="Commit, stash, or discard changes before syncing",
            code="worktree_dirty",
        ))
        return

    # Determine which source branch to ff from. Default: whatever the
    # source repo is currently on (usually main).
    if not source_branch:
        rev = _git(source, "rev-parse", "--abbrev-ref", "HEAD")
        source_branch = rev.stdout.strip()
        if not source_branch or source_branch == "HEAD":
            out.error(WsError(
                f"Source repo {source} has detached HEAD; cannot infer branch",
                fix="Pass --source-branch, or check out a branch in the source repo",
                code="source_detached",
            ))
            return

    # Ensure `source` remote points at ws.repo_path. Origin was rewritten
    # to the external URL at clone time (see checkout._rewrite_origin), so
    # we need a separate remote to fetch from the local source.
    remotes = _git(worktree, "remote").stdout.split()
    if "source" not in remotes:
        _git(worktree, "remote", "add", "source", str(source))
    else:
        # Update URL in case repo_path changed.
        _git(worktree, "remote", "set-url", "source", str(source))

    try:
        _git(worktree, "fetch", "source", source_branch)
    except subprocess.CalledProcessError as e:
        out.error(WsError(
            f"git fetch failed: {e.stderr.strip()}",
            fix=f"Check that branch '{source_branch}' exists in {source}",
            code="fetch_failed",
        ))
        return

    before = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    target = _git(worktree, "rev-parse", f"source/{source_branch}").stdout.strip()

    if before == target:
        out.success(
            {"name": name, "synced": False, "head": before,
             "reason": "already_up_to_date", "source_branch": source_branch},
            human_lines=[f"{name}: already at source/{source_branch} ({before[:12]})"],
        )
        return

    # Fast-forward only. If branches have diverged, surface and let the
    # user decide (merge, rebase, or commit from the worktree side).
    merge = _git(worktree, "merge", "--ff-only", f"source/{source_branch}", check=False)
    if merge.returncode != 0:
        out.error(WsError(
            f"ff-only merge refused (branches diverged): {merge.stderr.strip()}",
            fix=(f"cd {worktree} && git merge source/{source_branch}  "
                 f"# or: git rebase source/{source_branch}"),
            code="merge_diverged",
        ))
        return

    after = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    log = _git(worktree, "log", "--oneline", f"{before}..{after}").stdout.strip()
    num_commits = len(log.splitlines()) if log else 0

    out.success(
        {
            "name": name,
            "synced": True,
            "before": before,
            "after": after,
            "source_branch": source_branch,
            "commits": num_commits,
        },
        human_lines=[
            f"{name}: {before[:12]} → {after[:12]} ({num_commits} commit{'s' if num_commits != 1 else ''})",
            *[f"  {line}" for line in log.splitlines()[:10]],
            *([f"  … {num_commits - 10} more"] if num_commits > 10 else []),
            *([f"next: `ws stop {name} && ws create {name}` to rebuild on new source"]
              if ws.state == "running" else []),
        ],
    )
