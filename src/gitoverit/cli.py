from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from traceback import TracebackException
from typing import Annotated, List, Optional

import typer
from rich.console import Console

from .output import render_json, render_table
from .progress import RichHook, SilentHook
from .reporting import RepoReport, collect_reports_parallel, relativize

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


class ErrorFormat(str, Enum):
    IGNORE = "ignore"
    SHORT = "short"
    LONG = "long"


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
    parallel: Optional[int] = typer.Option(
        None,
        "--parallel", "-p",
        help="Number of parallel workers (default: auto-detect, 0 = sequential mode)"
    ),
    errorfmt: ErrorFormat = typer.Option(
        ErrorFormat.SHORT,
        "--errorfmt",
        case_sensitive=False,
        help="Error display: ignore (silent), short (one line per error), long (full traceback, skip table)"
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    hook: RichHook | SilentHook = RichHook(console) if _stdout_is_tty() else SilentHook()

    reports = collect_reports_parallel(
        dirs,
        fetch=fetch,
        dirty_only=dirty_only,
        hook=hook,
        max_workers=parallel,  # None means auto-detect, 0 means sequential, N means N workers
    )

    errors = hook.get_errors()

    _sort_reports(reports, sort=sort, reverse=reverse)

    # Handle error display based on errorfmt
    if errorfmt is ErrorFormat.LONG and errors:
        # Long format with errors: skip table, show full tracebacks
        _render_errors_long(console, errors)
    else:
        # Show table (ignore, short, or long with no errors)
        if output_format is OutputFormat.JSON:
            typer.echo(render_json(reports))
        else:
            render_table(console, reports)

        # Short format: show one-line errors after table
        if errorfmt is ErrorFormat.SHORT and errors:
            _render_errors_short(console, errors)


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


def _render_errors_short(
    console: Console, errors: list[tuple[Path, TracebackException | None]]
) -> None:
    """Render one line per error after the table."""
    console.print()
    for path, tb in errors:
        display_path = relativize(path)
        if tb is not None:
            exc_type = tb.exc_type.__name__ if tb.exc_type else "Error"
            exc_msg = str(tb).split("\n")[-1].strip() if str(tb) else ""
            console.print(f"[red]Error:[/red] {display_path}: {exc_type}: {exc_msg}")
        else:
            console.print(f"[red]Error:[/red] {display_path}: unknown error")


def _render_errors_long(
    console: Console, errors: list[tuple[Path, TracebackException | None]]
) -> None:
    """Render full tracebacks for all errors, skip the table."""
    for path, tb in errors:
        display_path = relativize(path)
        console.print(f"[red bold]Error in {display_path}:[/red bold]")
        if tb is not None:
            for line in tb.format():
                console.print(line, end="")
        else:
            console.print("  unknown error")
        console.print()


def main() -> None:
    APP()


__all__ = ["APP", "main"]
