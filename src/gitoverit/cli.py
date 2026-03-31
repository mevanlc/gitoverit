from __future__ import annotations

import os
import re
import sys
from enum import Enum
from pathlib import Path
from traceback import TracebackException
from typing import Annotated, List, Optional

import typer
from rich.console import Console
from simpleeval import DEFAULT_NAMES, SimpleEval

from .output import parse_columns, render_json, render_table
from .progress import RichHook, SilentHook
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


_WHERE_HELP = """\
Filter expressions for --where / -w

  Expressions use Python-like syntax to filter which repositories appear
  in the output.  Each expression is evaluated per repo; rows where the
  expression is falsy are excluded.

VARIABLES

  Strings:
    path     Absolute path of the repository
    dir      Display path of the repository (relative to cwd)
    status   Rendered status text, e.g. "3m 1u 2↑" or "clean"
    branch   Current branch name, e.g. "main"
    remote   Tracking remote ref, e.g. "origin/main", or "-"
    url      Simplified remote URL, or "-"
    ident    Git committer identity, e.g. "Alice <alice@x.co>", or ""

  Numbers:
    mtime      Latest worktree modification time as epoch float, or 0.0
    ahead      Commits ahead of tracking branch
    behind     Commits behind tracking branch
    modified   Count of modified files
    untracked  Count of untracked files
    deleted    Count of deleted files

  Booleans:
    dirty    True if the repo has uncommitted local changes

OPERATORS

  ==  !=  <  <=  >  >=    Comparisons
  and  or  not            Boolean logic
  in                      Substring / membership test
  +                       String concatenation (on strings)

  Parentheses for grouping: (a or b) and c

STRING METHODS

  String variables support .rx() and .rxi() for regex matching:

    .rx(pattern)     Regex search (case-sensitive), returns bool
    .rxi(pattern)    Regex search (case-insensitive), returns bool

  Standard Python str methods also work: .startswith(), .endswith(),
  .lower(), .upper(), .strip(), etc.

FUNCTIONS

  rx(string, pattern)    Regex search (case-sensitive)
  rxi(string, pattern)   Regex search (case-insensitive)

EXAMPLES

  Show only dirty repos:              'dirty'
  Clean repos only:                   'not dirty'
  Repos on a non-main branch:         'branch != "main"'
  Repos with unpushed commits:        'ahead > 0'
  Repos that are behind:              'behind > 0'
  Repos with untracked files:         'untracked > 0'
  Repos with no remote tracking:      'remote == "-"'
  Directory prefix:                   'dir.startswith("work/")'
  URL contains "myorg":               '"myorg" in url'
  Feature branch (regex):             'branch.rx("^feature/")'
  Branch starts with release
    or hotfix:                        'branch.rx("^(release|hotfix)")'
  Author match
    (case-insensitive regex):         'ident.rxi("alice")'
  Combine conditions,
    dirty repos not on main:          'dirty and branch != "main"'
  Recent mtime (epoch seconds,
    use `date +%s` for current):      'mtime > 1742500000'

NOTES

  Expressions are evaluated with simpleeval (sandboxed). Standard
  Python builtins are not available; use the variables and functions
  listed above.

  The --where filter runs after --dirty-only and sorting, so all
  three can be combined.

  The --print / -p option uses the same variables and expression
  language to evaluate and print a value per repo.
"""


def _show_help_where(value: bool) -> None:
    if value:
        typer.echo(_WHERE_HELP, color=False)
        raise typer.Exit()


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
        False, "-d", "--dirty-only", help="Display only repositories with uncommitted local changes."
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
        "-j", "--jobs",
        help="Number of parallel workers (default: auto-detect, 0 = sequential mode)"
    ),
    print_expr: Optional[str] = typer.Option(
        None,
        "-p", "--print",
        help="Evaluate expression per repo and print results, one per line. "
        "Same variables as --where, plus path.",
    ),
    print0: bool = typer.Option(
        False,
        "-0", "--print0",
        help="With --print, use null bytes instead of newlines as delimiters.",
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
        "Columns: dir,status,branch_remote,branch,remote,url,mtime,ident",
    ),
    where: Optional[str] = typer.Option(
        None,
        "-w", "--where",
        help="Filter expression. Variables: path, dir, status, branch, remote, url, ident, mtime, "
        "dirty, ahead, behind, modified, untracked, deleted.",
    ),
    show_errors: bool = typer.Option(
        False,
        "--errors",
        help="Print error details to stderr after all output is complete.",
    ),
    _help_where: Optional[bool] = typer.Option(
        None,
        "--help-where",
        callback=_show_help_where,
        is_eager=True,
        help="Show detailed help for --where filter expressions and exit.",
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    if _stdout_is_tty():
        hook = RichHook(console)
    elif show_errors:
        hook = SilentHook()
    else:
        hook = None

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

    if print_expr is not None:
        _print_reports(reports, print_expr, null_delimited=print0)
    elif output_format is OutputFormat.JSON:
        typer.echo(render_json(reports))
    else:
        columns = parse_columns(columns_spec) if columns_spec else None
        minimize_chars = table_algo is TableAlgo.CHAR
        render_table(console, reports, minimize_chars=minimize_chars, columns=columns)

    if show_errors and hook:
        _emit_errors(hook.get_errors())


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
    mtime = report.latest_mtime or 0.0
    names = dict(DEFAULT_NAMES)
    names.update(
        path=_RxStr(str(report.path)),
        dir=_RxStr(report.display_path),
        status=_RxStr(render_status_segments(report.status_segments)),
        branch=_RxStr(report.branch),
        remote=_RxStr(report.remote),
        url=_RxStr(report.remote_url),
        ident=_RxStr(report.ident or ""),
        mtime=mtime,
        latest_mtime=mtime,
        dirty=report.dirty,
        ahead=report.ahead,
        behind=report.behind,
        modified=report.modified,
        untracked=report.untracked,
        deleted=report.deleted,
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


def _print_reports(
    reports: list[RepoReport], expr: str, *, null_delimited: bool
) -> None:
    evaluator = SimpleEval()
    evaluator.functions["rx"] = _rx
    evaluator.functions["rxi"] = _rxi
    parsed = evaluator.parse(expr)
    end = "\0" if null_delimited else "\n"
    for report in reports:
        evaluator.names = _report_names(report)
        value = evaluator.eval(expr, previously_parsed=parsed)
        sys.stdout.write(f"{value}{end}")
    sys.stdout.flush()


def _sort_reports(reports: List[RepoReport], *, sort: SortMode, reverse: bool) -> None:
    if sort is SortMode.MTIME:
        reports.sort(key=lambda report: report.latest_mtime or 0.0, reverse=reverse)
    elif sort is SortMode.AUTHOR:
        reports.sort(
            key=lambda report: (report.ident or "").lower(),
            reverse=reverse,
        )
    elif reverse:
        reports.reverse()


def _emit_errors(errors: list[tuple[Path, TracebackException | None]]) -> None:
    if not errors:
        return
    stderr = Console(stderr=True, highlight=False)
    stderr.print(f"\n[bold red]{len(errors)} error(s) during statusing:[/bold red]")
    for path, tb in errors:
        stderr.print(f"\n[bold]{path}[/bold]")
        if tb is not None:
            for line in tb.format():
                stderr.print(line, end="")
        else:
            stderr.print("  (no traceback available)")


def _stdout_is_tty() -> bool:
    try:
        return os.isatty(sys.stdout.fileno())
    except Exception:
        return False


def main() -> None:
    APP()


__all__ = ["APP", "main"]
