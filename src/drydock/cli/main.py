"""ws — Drydock workspace orchestration CLI."""

import click

from drydock.core.errors import WsError
from drydock.core.registry import Registry
from drydock.output.formatter import Output


@click.group()
@click.option("--json", "force_json", is_flag=True, help="Force JSON output")
@click.option("--dry-run", is_flag=True, help="Preview without executing")
@click.pass_context
def cli(ctx, force_json, dry_run):
    """Drydock workspace orchestration."""
    ctx.ensure_object(dict)
    ctx.obj["output"] = Output(force_json=force_json)
    ctx.obj["dry_run"] = dry_run
    try:
        ctx.obj["registry"] = Registry()
    except Exception as e:
        ctx.obj["output"].error(
            WsError(f"Failed to open registry: {e}", fix="Check ~/.drydock/ permissions")
        )


# Import and register commands
from drydock.cli.create import create  # noqa: E402
from drydock.cli.list_cmd import list_cmd  # noqa: E402
from drydock.cli.inspect_cmd import inspect_cmd  # noqa: E402
from drydock.cli.stop import stop  # noqa: E402
from drydock.cli.destroy import destroy  # noqa: E402
from drydock.cli.attach import attach  # noqa: E402

cli.add_command(create)
cli.add_command(list_cmd, name="list")
cli.add_command(inspect_cmd, name="inspect")
cli.add_command(stop)
cli.add_command(destroy)
cli.add_command(attach)
