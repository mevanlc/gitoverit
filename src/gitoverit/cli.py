from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, List

import typer
from rich.console import Console

from .output import render_json, render_table
from .progress import RichHook
from .reporting import RepoReport, collect_reports

console = Console()

APP = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"


class SortMode(str, Enum):
    MTIME = "mtime"
    AUTHOR = "author"
    NONE = "none"


@APP.command()
def cli(
    dirs: Annotated[List[Path], typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, writable=False
    )] = [Path.cwd()],
    fetch: bool = typer.Option(
        False, "--fetch", help="Run git fetch --all for each repository before inspection."
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TABLE, "--format", case_sensitive=False, help="Choose output format."
    ),
    dirty_only: bool = typer.Option(
        False, "--dirty-only", help="Display only repositories with local or remote changes."
    ),
    sort: SortMode = typer.Option(
        SortMode.MTIME,
        "--sort",
        case_sensitive=False,
        help="Sort repositories by mtime (default), author, or disable sorting with none",
    ),
    reverse: bool = typer.Option(
        False,
        "--reverse",
        help="Reverse sort order when a sort mode is active.",
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    hook = RichHook(console) if _stdout_is_tty() else None
    reports = collect_reports(dirs, fetch=fetch, dirty_only=dirty_only, hook=hook)

    _sort_reports(reports, sort=sort, reverse=reverse)

    if output_format is OutputFormat.JSON:
        typer.echo(render_json(reports))
    else:
        render_table(console, reports)


def _sort_reports(reports: List[RepoReport], *, sort: SortMode, reverse: bool) -> None:
    if sort is SortMode.MTIME:
        reports.sort(key=lambda report: report.latest_mtime or 0.0, reverse=not reverse)
    elif sort is SortMode.AUTHOR:
        reports.sort(
            key=lambda report: (report.ident or "").lower(),
            reverse=reverse,
        )
    elif reverse:
        reports.reverse()


def _stdout_is_tty() -> bool:
    try:
        return os.isatty(sys.stdout.fileno())
    except Exception:
        return False


def main() -> None:
    APP()


__all__ = ["APP", "main"]
