"""Output formatting — JSON for agents, human-readable for terminals."""

import json
import sys

import click

from drydock.core import WsError


class Output:
    def __init__(self, force_json: bool = False):
        self.json_mode = force_json or not sys.stdout.isatty()

    def success(self, data: dict | list, human_lines: list[str] | None = None):
        if self.json_mode:
            click.echo(json.dumps(data, indent=2, default=str))
        elif human_lines:
            for line in human_lines:
                click.echo(line)
        else:
            click.echo(json.dumps(data, indent=2, default=str))

    def error(self, err: WsError):
        if self.json_mode:
            click.echo(json.dumps(err.to_dict(), indent=2), err=True)
        else:
            click.echo(f"error: {err.message}", err=True)
            if err.fix:
                click.echo(f"  fix: {err.fix}", err=True)
            if err.context:
                for k, v in err.context.items():
                    click.echo(f"  {k}: {v}", err=True)
        raise SystemExit(1)

    def table(self, rows: list[dict], columns: list[str]):
        if self.json_mode:
            click.echo(json.dumps(rows, indent=2, default=str))
            return

        if not rows:
            click.echo("(no drydocks)")
            return

        # Calculate column widths
        widths = {col: len(col) for col in columns}
        for row in rows:
            for col in columns:
                widths[col] = max(widths[col], len(str(row.get(col, ""))))

        # Header
        header = "  ".join(col.upper().ljust(widths[col]) for col in columns)
        click.echo(header)
        click.echo("  ".join("-" * widths[col] for col in columns))

        # Rows
        for row in rows:
            line = "  ".join(
                str(row.get(col, "")).ljust(widths[col]) for col in columns
            )
            click.echo(line)
