from __future__ import annotations

import fnmatch
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, TypeAlias

import typer
from rich.console import Console
from simpleeval import DEFAULT_NAMES, SimpleEval

from .branches import BranchCollectionError, BranchReport, collect_branch_reports
from .branches_output import parse_columns, render_json, render_table

console = Console()

SortKey: TypeAlias = str | int

APP = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


class SortMode(str, Enum):
    BRANCH = "branch"
    DATE = "date"
    AUTHOR = "author"
    AHEAD = "ahead"
    BEHIND = "behind"
    STATUS = "status"
    UPSTREAM = "upstream"
    CURRENT = "current"
    HEAD = "head"
    SUBJECT = "subject"
    WORKTREE = "worktree"
    GONE = "gone"
    NONE = "none"


class TableAlgo(str, Enum):
    CELL = "cell"
    CHAR = "char"


_WHERE_HELP = """\
Filter expressions for `gitob --where / -w`

  Expressions use Python-like syntax and are evaluated once per branch row.
  Rows where the expression is falsy are excluded.

VARIABLES

  Strings:
    repo           Absolute path of the target repo worktree
    path           Alias for repo
    branch         Local branch name, e.g. "main"
    head           Short tip commit id
    upstream       Configured upstream, e.g. "origin/main", or "-"
    status         Rendered status text, e.g. "3m 1u 2↑" or "clean"
    worktree       Display value for the checked-out worktree: "here", path, or "-"
    worktree_path  Absolute path of the checked-out worktree, or ""
    author         Tip commit author identity
    subject        Tip commit subject line

  Numbers:
    ahead          Commits ahead of upstream
    behind         Commits behind upstream
    modified       Count of modified files in the branch's worktree (0 if none)
    untracked      Count of untracked files in the branch's worktree (0 if none)
    deleted        Count of deleted files in the branch's worktree (0 if none)
    date           Tip commit committer date as epoch seconds

  Booleans:
    current        True when this branch is checked out in the target worktree
    gone           True when upstream is configured but missing locally
    has_upstream   True when upstream is configured
    dirty          True when the branch's worktree has uncommitted changes

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

  Current branch only:               'current'
  Branches checked out somewhere:    'worktree != "-"'
  Branches with gone upstreams:      'gone'
  Branches ahead of upstream:        'ahead > 0'
  Branches without upstream:         'not has_upstream'
  Feature branches:                  'branch.rx("^feature/")'
  Recent commits (epoch seconds):    'date > 1770000000'
  Branches in linked worktrees:      'worktree not in ("-", "here")'

NOTES

  Expressions are evaluated with simpleeval (sandboxed). Standard Python
  builtins are not available; use the variables and functions listed above.

  `--print / -p` uses the same variables and expression language to print a
  value per branch row.
"""


def _show_help_where(value: bool) -> None:
    if value:
        typer.echo(_WHERE_HELP, color=False)
        raise typer.Exit()


@APP.command()
def branches(
    repo_dir: Annotated[Path, typer.Argument(
        exists=True, file_okay=False, dir_okay=True, writable=False
    )] = Path.cwd(),
    fetch: bool = typer.Option(
        False,
        "-f",
        "--fetch",
        help="Run git fetch --all --prune before collecting branch information.",
    ),
    json_output: bool = typer.Option(
        False,
        "-j",
        "--json",
        help="Output JSON instead of a table.",
    ),
    glob_pattern: str | None = typer.Option(
        None,
        "-g",
        "--glob",
        help="Only include branches whose name matches this glob pattern.",
    ),
    regex_pattern: str | None = typer.Option(
        None,
        "-r",
        "--regex",
        help="Only include branches whose name matches this regex.",
    ),
    sort: SortMode = typer.Option(
        SortMode.BRANCH,
        "-s",
        "--sort",
        case_sensitive=False,
        help="Sort branches by any supported field; default is branch.",
    ),
    reverse: bool = typer.Option(
        False,
        "--reverse",
        help="Reverse sort order when a sort mode is active.",
    ),
    print_expr: str | None = typer.Option(
        None,
        "-p",
        "--print",
        help="Evaluate expression per branch and print results, one per line.",
    ),
    print0: bool = typer.Option(
        False,
        "-0",
        "--print0",
        help="With --print, use null bytes instead of newlines as delimiters.",
    ),
    table_algo: TableAlgo = typer.Option(
        TableAlgo.CELL,
        "-a",
        "--table-algo",
        case_sensitive=False,
        help="Table column width autosizing algorithm.",
    ),
    columns_spec: str | None = typer.Option(
        None,
        "-c",
        "--columns",
        help=(
            "Comma-separated column spec: col to add, -col to remove, - to clear all. "
            "Columns: branch,head,upstream,status,ahead,behind,gone,current,"
            "worktree,date,author,subject"
        ),
    ),
    where: str | None = typer.Option(
        None,
        "-w",
        "--where",
        help=(
            "Filter expression. Variables: repo, path, branch, head, upstream, status, "
            "ahead, behind, modified, untracked, deleted, gone, current, dirty, "
            "has_upstream, worktree, worktree_path, author, date, subject."
        ),
    ),
    _help_where: bool | None = typer.Option(
        None,
        "--help-where",
        callback=_show_help_where,
        is_eager=True,
        help="Show detailed help for --where filter expressions and exit.",
    ),
) -> None:
    """Gather information about branches inside one repository."""

    try:
        reports = collect_branch_reports(repo_dir, fetch=fetch)
    except BranchCollectionError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if glob_pattern is not None:
        reports = [report for report in reports if fnmatch.fnmatch(report.branch, glob_pattern)]

    if regex_pattern is not None:
        pattern = re.compile(regex_pattern)
        reports = [report for report in reports if pattern.search(report.branch)]

    if where:
        reports = _filter_reports(reports, where)

    _sort_reports(reports, sort=sort, reverse=reverse)

    columns = parse_columns(columns_spec) if columns_spec else None
    if print_expr is not None:
        _print_reports(reports, print_expr, null_delimited=print0)
    elif json_output:
        typer.echo(render_json(reports, columns=columns))
    else:
        minimize_chars = table_algo is TableAlgo.CHAR
        render_table(console, reports, minimize_chars=minimize_chars, columns=columns)


class _RxStr(str):
    def rx(self, pattern: str) -> bool:
        return bool(re.search(pattern, self))

    def rxi(self, pattern: str) -> bool:
        return bool(re.search(pattern, self, re.IGNORECASE))


def _rx(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value))


def _rxi(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value, re.IGNORECASE))


def _report_names(report: BranchReport) -> dict[str, object]:
    from .branches_output import _status_plain_text

    names = dict(DEFAULT_NAMES)
    names.update(
        repo=_RxStr(str(report.repo_path)),
        path=_RxStr(str(report.repo_path)),
        branch=_RxStr(report.branch),
        head=_RxStr(report.head),
        upstream=_RxStr(report.upstream),
        status=_RxStr(_status_plain_text(report)),
        worktree=_RxStr(report.worktree_display),
        worktree_path=_RxStr(str(report.worktree_path) if report.worktree_path is not None else ""),
        author=_RxStr(report.author),
        subject=_RxStr(report.subject),
        ahead=report.ahead,
        behind=report.behind,
        modified=report.modified,
        untracked=report.untracked,
        deleted=report.deleted,
        date=report.date,
        current=report.current,
        gone=report.gone,
        dirty=report.dirty,
        has_upstream=report.has_upstream,
    )
    return names


def _filter_reports(reports: list[BranchReport], expr: str) -> list[BranchReport]:
    evaluator = SimpleEval()
    evaluator.functions["rx"] = _rx
    evaluator.functions["rxi"] = _rxi
    parsed = evaluator.parse(expr)
    result: list[BranchReport] = []
    for report in reports:
        evaluator.names = _report_names(report)
        if evaluator.eval(expr, previously_parsed=parsed):
            result.append(report)
    return result


def _print_reports(reports: list[BranchReport], expr: str, *, null_delimited: bool) -> None:
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


def _sort_reports(reports: list[BranchReport], *, sort: SortMode, reverse: bool) -> None:
    if sort is SortMode.NONE:
        if reverse:
            reports.reverse()
        return
    reports.sort(key=lambda report: _report_sort_key(report, sort), reverse=reverse)


def _report_sort_key(report: BranchReport, sort: SortMode) -> SortKey:
    if sort is SortMode.BRANCH:
        return report.branch.casefold()
    if sort is SortMode.DATE:
        return report.date
    if sort is SortMode.AUTHOR:
        return report.author.casefold()
    if sort is SortMode.AHEAD:
        return report.ahead
    if sort is SortMode.BEHIND:
        return report.behind
    if sort is SortMode.STATUS:
        from .branches_output import _status_plain_text

        return _status_plain_text(report).casefold()
    if sort is SortMode.UPSTREAM:
        return report.upstream.casefold()
    if sort is SortMode.CURRENT:
        return 0 if report.current else 1
    if sort is SortMode.HEAD:
        return report.head.casefold()
    if sort is SortMode.SUBJECT:
        return report.subject.casefold()
    if sort is SortMode.WORKTREE:
        return report.worktree_display.casefold()
    if sort is SortMode.GONE:
        return 1 if report.gone else 0
    raise ValueError(f"Unsupported sort mode: {sort}")


def main() -> None:
    APP()


__all__ = [
    "APP",
    "SortMode",
    "TableAlgo",
    "_filter_reports",
    "_print_reports",
    "_sort_reports",
    "main",
]
