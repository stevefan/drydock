"""ws — Drydock workspace orchestration CLI."""

import logging

import click

from drydock.core import WsError
from drydock.core.registry import Registry
from drydock.output.formatter import Output


@click.group()
@click.option("--json", "force_json", is_flag=True, help="Force JSON output")
@click.option("--dry-run", is_flag=True, help="Preview without executing")
@click.pass_context
def cli(ctx, force_json, dry_run):
    """Drydock workspace orchestration."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
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
from drydock.cli.list import list as list_cmd  # noqa: E402
from drydock.cli.inspect import inspect as inspect_cmd  # noqa: E402
from drydock.cli.stop import stop  # noqa: E402
from drydock.cli.destroy import destroy  # noqa: E402
from drydock.cli.attach import attach  # noqa: E402
from drydock.cli.exec import exec_cmd  # noqa: E402
from drydock.cli.status import status  # noqa: E402
from drydock.cli.secret import secret  # noqa: E402
from drydock.cli.tailnet import tailnet  # noqa: E402
from drydock.cli.host import host  # noqa: E402
from drydock.cli.daemon import daemon  # noqa: E402
from drydock.cli.upgrade import upgrade  # noqa: E402
from drydock.cli.new import new  # noqa: E402
from drydock.cli.audit import audit  # noqa: E402
from drydock.cli.schedule import schedule  # noqa: E402
from drydock.cli.overlay import overlay  # noqa: E402
from drydock.cli.project import project  # noqa: E402

cli.add_command(create)
cli.add_command(upgrade)
cli.add_command(new)
cli.add_command(audit)
cli.add_command(list_cmd, name="list")
cli.add_command(inspect_cmd, name="inspect")
cli.add_command(stop)
cli.add_command(destroy)
cli.add_command(attach)
cli.add_command(exec_cmd, name="exec")
cli.add_command(status)
cli.add_command(secret)
cli.add_command(tailnet)
cli.add_command(host)
cli.add_command(daemon)
cli.add_command(schedule)
cli.add_command(overlay)
cli.add_command(project)
