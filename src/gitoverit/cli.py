from __future__ import annotations

import os
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich.console import Console
from simpleeval import DEFAULT_NAMES, SimpleEval

from .output import parse_columns, render_json, render_table
from .progress import RichHook
from .reporting import RepoReport, collect_reports_parallel, render_status_segments

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


class TableAlgo(str, Enum):
    CELL = "cell"
    CHAR = "char"


@APP.command()
def cli(
    dirs: Annotated[List[Path], typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, writable=False
    )] = [Path.cwd()],
    fetch: bool = typer.Option(
        False, "-f", "--fetch", help="Run git fetch --all for each repository before inspection."
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TABLE, "-o", "--format", case_sensitive=False, help="Choose output format."
    ),
    dirty_only: bool = typer.Option(
        False, "-d", "--dirty-only", help="Display only repositories with local or remote changes."
    ),
    sort: SortMode = typer.Option(
        SortMode.MTIME,
        "-s", "--sort",
        case_sensitive=False,
        help="Sort repositories by mtime (default), author, or disable sorting with none",
    ),
    reverse: bool = typer.Option(
        False,
        "-r", "--reverse",
        help="Reverse sort order when a sort mode is active.",
    ),
    parallel: Optional[int] = typer.Option(
        None,
        "-p", "--parallel",
        help="Number of parallel workers (default: auto-detect, 0 = sequential mode)"
    ),
    table_algo: TableAlgo = typer.Option(
        TableAlgo.CELL,
        "-a", "--table-algo",
        case_sensitive=False,
        help="Table column width autosizing algorithm",
    ),
    columns_spec: Optional[str] = typer.Option(
        None,
        "-c", "--columns",
        help="Comma-separated column spec: col to add, -col to remove, - to clear all. "
        "Columns: dir,status,branch,remote,url,ident",
    ),
    where: Optional[str] = typer.Option(
        None,
        "-w", "--where",
        help="Filter expression. Variables: dir, status, branch, remote, url, ident, dirty.",
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    hook = RichHook(console) if _stdout_is_tty() else None

    reports = collect_reports_parallel(
        dirs,
        fetch=fetch,
        dirty_only=dirty_only,
        hook=hook,
        max_workers=parallel,  # None means auto-detect, 0 means sequential, N means N workers
    )

    _sort_reports(reports, sort=sort, reverse=reverse)

    if where:
        reports = _filter_reports(reports, where)

    columns = parse_columns(columns_spec) if columns_spec else None

    if output_format is OutputFormat.JSON:
        typer.echo(render_json(reports))
    else:
        minimize_chars = table_algo is TableAlgo.CHAR
        render_table(console, reports, minimize_chars=minimize_chars, columns=columns)


class _RxStr(str):
    """str subclass with .rx() and .irx() methods for regex matching in --where expressions."""

    def rx(self, pattern: str) -> bool:
        return bool(re.search(pattern, self))

    def rxi(self, pattern: str) -> bool:
        return bool(re.search(pattern, self, re.IGNORECASE))


def _rx(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value))


def _rxi(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value, re.IGNORECASE))


def _report_names(report: RepoReport) -> dict[str, object]:
    names = dict(DEFAULT_NAMES)
    names.update(
        dir=_RxStr(report.display_path),
        status=_RxStr(render_status_segments(report.status_segments)),
        branch=_RxStr(report.branch),
        remote=_RxStr(report.remote),
        url=_RxStr(report.remote_url),
        ident=_RxStr(report.ident or ""),
        dirty=report.dirty,
    )
    return names


def _filter_reports(reports: list[RepoReport], expr: str) -> list[RepoReport]:
    evaluator = SimpleEval()
    evaluator.functions["rx"] = _rx
    evaluator.functions["rxi"] = _rxi
    parsed = evaluator.parse(expr)
    result: list[RepoReport] = []
    for report in reports:
        evaluator.names = _report_names(report)
        if evaluator.eval(expr, previously_parsed=parsed):
            result.append(report)
    return result


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
