"""ws new — scaffold a drydock-flavored devcontainer in a project repo."""

import logging
from importlib import resources
from pathlib import Path

import click

from drydock.core import WsError

logger = logging.getLogger(__name__)

# Worker (default) template — drydock-base + project tooling stub.
WORKER_TEMPLATE_PACKAGE = "drydock.templates.devcontainer_drydock"
WORKER_TEMPLATE_FILES = ("Dockerfile", "devcontainer.json")
WORKER_DEFAULT_BASE_TAG = "v1.0.7"

# Auditor (Phase PA3.4) template — role-locked, references the baked
# drydock-port-auditor image directly. Dockerfile.example is included
# as an opt-in extension point for harnesses that need custom services
# (state machine, multi-process supervisor) but isn't wired into
# devcontainer.json by default — most Auditors don't need extension.
AUDITOR_TEMPLATE_PACKAGE = "drydock.templates.port_auditor"
AUDITOR_TEMPLATE_FILES = ("devcontainer.json", "Dockerfile.example")
AUDITOR_DEFAULT_BASE_TAG = "v0.1.0"

DRYDOCK_SUBPATH = ".devcontainer/drydock"
PROJECTS_DIR = Path.home() / ".drydock" / "projects"


def _render(text: str, project_name: str, base_tag: str) -> str:
    return text.replace("{{ project_name }}", project_name).replace("{{ base_tag }}", base_tag)


def _worker_project_yaml(repo_path: Path) -> str:
    return (
        f"repo_path: {repo_path}\n"
        f"devcontainer_subpath: {DRYDOCK_SUBPATH}\n"
    )


def _auditor_project_yaml(repo_path: Path, base_tag: str) -> str:
    """Role-locked YAML body matching role_validator's constraints.

    The values here are the minimum that PASS the validator; principal
    can edit afterwards (within the same constraints) for prompts,
    deskwatch probes, telegram bot wiring, etc.
    """
    return (
        f"# Port Auditor — role-locked drydock. Constraints enforced by\n"
        f"# core/auditor/role_validator.py. Loosening any value below\n"
        f"# requires editing the validator (deliberate, code-level).\n"
        f"role: auditor\n"
        f"\n"
        f"repo_path: {repo_path}\n"
        f"devcontainer_subpath: {DRYDOCK_SUBPATH}\n"
        f"image: ghcr.io/stevefan/drydock-port-auditor:{base_tag}\n"
        f"\n"
        f"firewall_extra_domains:\n"
        f"  - api.anthropic.com\n"
        f"  - api.telegram.org\n"
        f"\n"
        f"resources_hard:\n"
        f"  cpus: 1.0\n"
        f"  memory: 1g\n"
        f"  pids: 256\n"
    )


@click.command(name="new")
@click.argument("project")
@click.option("--repo-path", default=".", help="Project directory (default: cwd)")
@click.option(
    "--role", "role",
    type=click.Choice(["worker", "auditor"]), default="worker", show_default=True,
    help="Drydock role. 'auditor' uses the port-auditor template + role-locked project YAML.",
)
@click.option("--base-tag", default=None,
              help="Image tag override. Defaults: worker→drydock-base v1.0.7, auditor→drydock-port-auditor v0.1.0.")
@click.option("--write-project-yaml/--no-write-project-yaml", default=True,
              help="Also write ~/.drydock/projects/<project>.yaml")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.pass_context
def new(ctx, project, repo_path, role, base_tag, write_project_yaml, force):
    """Scaffold .devcontainer/drydock/ in PROJECT's repo from a starter template.

    For worker drydocks (default): scaffolds Dockerfile + devcontainer.json
    rooted at drydock-base; project YAML is a minimal stub.

    For auditor drydocks (--role auditor): scaffolds a thin
    devcontainer.json that references the baked drydock-port-auditor
    image directly + a role-locked project YAML that passes
    core/auditor/role_validator.py. A Dockerfile.example is included
    as an opt-in extension point if you later need to bake harness
    services (state machine, multi-process supervisor, etc.).
    """
    out = ctx.obj["output"]
    dry_run = ctx.obj["dry_run"]

    target_repo = Path(repo_path).expanduser().resolve()
    if not target_repo.is_dir():
        out.error(WsError(
            f"Repo path does not exist: {target_repo}",
            fix=f"mkdir -p {target_repo} or pass --repo-path to an existing dir",
        ))
        return

    if role == "auditor":
        template_package = AUDITOR_TEMPLATE_PACKAGE
        template_files = AUDITOR_TEMPLATE_FILES
        effective_base_tag = base_tag or AUDITOR_DEFAULT_BASE_TAG
        project_yaml_body = _auditor_project_yaml(target_repo, effective_base_tag)
    else:
        template_package = WORKER_TEMPLATE_PACKAGE
        template_files = WORKER_TEMPLATE_FILES
        effective_base_tag = base_tag or WORKER_DEFAULT_BASE_TAG
        project_yaml_body = _worker_project_yaml(target_repo)

    drydock_dir = target_repo / DRYDOCK_SUBPATH
    written: list[str] = []
    skipped: list[str] = []
    existing_blockers: list[str] = []

    for fname in template_files:
        target = drydock_dir / fname
        if target.exists() and not force:
            existing_blockers.append(str(target))

    if existing_blockers and not force:
        out.error(WsError(
            f"Refusing to overwrite existing files: {', '.join(existing_blockers)}",
            fix="Pass --force to overwrite",
        ))
        return

    if dry_run:
        out.success(
            {
                "dry_run": True,
                "project": project,
                "role": role,
                "would_write": [str(drydock_dir / f) for f in template_files],
                "would_write_project_yaml": (
                    str(PROJECTS_DIR / f"{project}.yaml") if write_project_yaml else None
                ),
                "base_tag": effective_base_tag,
            },
            human_lines=[
                f"Would scaffold {drydock_dir}/ ({', '.join(template_files)})",
                f"  role: {role}",
                f"  base tag: {effective_base_tag}",
                (f"Would write {PROJECTS_DIR / f'{project}.yaml'}"
                 if write_project_yaml else "Would skip project YAML"),
            ],
        )
        return

    drydock_dir.mkdir(parents=True, exist_ok=True)
    for fname in template_files:
        text = resources.files(template_package).joinpath(fname).read_text(encoding="utf-8")
        rendered = _render(text, project_name=project, base_tag=effective_base_tag)
        target = drydock_dir / fname
        target.write_text(rendered)
        written.append(str(target))

    project_yaml_path: str | None = None
    if write_project_yaml:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        py = PROJECTS_DIR / f"{project}.yaml"
        if py.exists():
            logger.warning("Project YAML already exists at %s; skipping", py)
            skipped.append(str(py))
        else:
            py.write_text(project_yaml_body)
            project_yaml_path = str(py)

    out.success(
        {
            "project": project,
            "role": role,
            "written": written,
            "project_yaml": project_yaml_path,
            "skipped": skipped,
            "base_tag": effective_base_tag,
        },
        human_lines=[
            f"scaffolded {drydock_dir} (role={role})",
            *(f"  + {p}" for p in written),
            *(f"  + {project_yaml_path}" for _ in [0] if project_yaml_path),
            *(f"  ~ skipped {p} (already exists)" for p in skipped),
        ],
    )
