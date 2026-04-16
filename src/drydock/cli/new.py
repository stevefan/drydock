"""ws new — scaffold a drydock-flavored devcontainer in a project repo."""

import logging
from importlib import resources
from pathlib import Path

import click

from drydock.core import WsError

logger = logging.getLogger(__name__)

TEMPLATE_PACKAGE = "drydock.templates.devcontainer_drydock"
TEMPLATE_FILES = ("Dockerfile", "devcontainer.json")
DRYDOCK_SUBPATH = ".devcontainer/drydock"
PROJECTS_DIR = Path.home() / ".drydock" / "projects"


def _render(text: str, project_name: str, base_tag: str) -> str:
    return text.replace("{{ project_name }}", project_name).replace("{{ base_tag }}", base_tag)


def _project_yaml_body(repo_path: Path) -> str:
    return (
        f"repo_path: {repo_path}\n"
        f"devcontainer_subpath: {DRYDOCK_SUBPATH}\n"
    )


@click.command(name="new")
@click.argument("project")
@click.option("--repo-path", default=".", help="Project directory (default: cwd)")
@click.option("--base-tag", default="v1.0.7", help="drydock-base tag (default: v1.0.7)")
@click.option("--write-project-yaml/--no-write-project-yaml", default=True,
              help="Also write ~/.drydock/projects/<project>.yaml")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.pass_context
def new(ctx, project, repo_path, base_tag, write_project_yaml, force):
    """Scaffold .devcontainer/drydock/ in PROJECT's repo from a starter template."""
    out = ctx.obj["output"]
    dry_run = ctx.obj["dry_run"]

    target_repo = Path(repo_path).expanduser().resolve()
    if not target_repo.is_dir():
        out.error(WsError(
            f"Repo path does not exist: {target_repo}",
            fix=f"mkdir -p {target_repo} or pass --repo-path to an existing dir",
        ))
        return

    drydock_dir = target_repo / DRYDOCK_SUBPATH
    written: list[str] = []
    skipped: list[str] = []
    existing_blockers: list[str] = []

    for fname in TEMPLATE_FILES:
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
                "would_write": [str(drydock_dir / f) for f in TEMPLATE_FILES],
                "would_write_project_yaml": (
                    str(PROJECTS_DIR / f"{project}.yaml") if write_project_yaml else None
                ),
                "base_tag": base_tag,
            },
            human_lines=[
                f"Would scaffold {drydock_dir}/{{Dockerfile,devcontainer.json}}",
                f"  base tag: {base_tag}",
                (f"Would write {PROJECTS_DIR / f'{project}.yaml'}"
                 if write_project_yaml else "Would skip project YAML"),
            ],
        )
        return

    drydock_dir.mkdir(parents=True, exist_ok=True)
    for fname in TEMPLATE_FILES:
        text = resources.files(TEMPLATE_PACKAGE).joinpath(fname).read_text(encoding="utf-8")
        rendered = _render(text, project_name=project, base_tag=base_tag)
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
            py.write_text(_project_yaml_body(target_repo))
            project_yaml_path = str(py)

    out.success(
        {
            "project": project,
            "written": written,
            "project_yaml": project_yaml_path,
            "skipped": skipped,
            "base_tag": base_tag,
        },
        human_lines=[
            f"scaffolded {drydock_dir}",
            *(f"  + {p}" for p in written),
            *(f"  + {project_yaml_path}" for _ in [0] if project_yaml_path),
            *(f"  ~ skipped {p} (already exists)" for p in skipped),
        ],
    )
