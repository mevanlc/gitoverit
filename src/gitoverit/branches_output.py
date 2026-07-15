from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Callable, Sequence

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from .branches import BranchReport
from .output.table import AutoTable, ResponsiveCell, _status_key_exceptional, _status_key_main


def render_json(reports: Sequence[BranchReport], *, columns: list[str] | None = None) -> str:
    payload = [_json_payload(report, columns=columns) for report in reports]
    return json.dumps(payload, indent=2, sort_keys=True)


def render_table(
    console: Console,
    reports: Sequence[BranchReport],
    *,
    minimize_chars: bool = False,
    columns: list[str] | None = None,
) -> None:
    active_columns = columns if columns is not None else DEFAULT_COLUMNS
    table = AutoTable(width="fill", minimize_chars=minimize_chars)

    for column in active_columns:
        header_factory, priority = _COLUMN_DEFS[column]
        table.add_column(header_factory(), priority=priority)

    for report in reports:
        table.add_row(*[_row_value(column, report) for column in active_columns])

    console.print(table)

    if reports and "status" in active_columns:
        console.print(_status_key_main(show_checked=True, show_unchecked=False))
        show_exceptional = any(
            any(seg[0] == "!" for seg in report.status_segments) for report in reports
        )
        if show_exceptional:
            console.print(_status_key_exceptional(conflicts_checked=True))


def parse_columns(spec: str) -> list[str]:
    result = list(DEFAULT_COLUMNS)
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token == "-":
            result.clear()
        elif token.startswith("-"):
            name = token[1:]
            if name not in _COLUMN_DEFS:
                raise ValueError(f"Unknown column: {name!r}")
            if name in result:
                result.remove(name)
        else:
            if token not in _COLUMN_DEFS:
                raise ValueError(f"Unknown column: {token!r}")
            if token in result:
                result.remove(token)
            result.append(token)
    return result


def _json_payload(report: BranchReport, *, columns: list[str] | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "repo_path": str(report.repo_path),
        "branch": report.branch,
        "head": report.head,
        "upstream": report.upstream,
        "has_upstream": report.has_upstream,
        "ahead": report.ahead,
        "behind": report.behind,
        "status": _status_plain_text(report),
        "gone": report.gone,
        "current": report.current,
        "worktree": report.worktree_display,
        "worktree_path": str(report.worktree_path) if report.worktree_path is not None else None,
        "author": report.author,
        "date": report.date,
        "subject": report.subject,
        "modified": report.modified,
        "untracked": report.untracked,
        "deleted": report.deleted,
        "dirty": report.dirty,
    }
    if columns is None:
        return payload
    return {column: payload[column] for column in columns if column in payload}


def _responsive(value: ResponsiveCell | Text | str) -> ResponsiveCell:
    if isinstance(value, ResponsiveCell):
        return value
    if isinstance(value, Text):
        return ResponsiveCell(variants=(value,))
    return ResponsiveCell(variants=(Text(str(value)),))


def _header_cell(*labels: str) -> ResponsiveCell:
    return ResponsiveCell(variants=tuple(Text(label) for label in labels))


def _bool_cell(value: bool) -> ResponsiveCell:
    return _responsive(Text("*" if value else "-"))


def _date_cell(value: int) -> ResponsiveCell:
    if value <= 0:
        return _responsive("-")
    timestamp = datetime.fromtimestamp(value)
    return ResponsiveCell(
        variants=(
            Text(timestamp.strftime("%Y-%m-%d %H:%M")),
            Text(timestamp.strftime("%Y-%m-%d")),
            Text(timestamp.strftime("%m-%d")),
            Text(timestamp.strftime("%m%d")),
        )
    )


def _empty_status_text(report: BranchReport) -> Text:
    # A checked-out branch with no dirt/drift is "clean"; a bare ref that
    # matches its upstream is "=".
    if report.worktree_path is not None:
        return Text("clean", style="green")
    return Text("=")


def _render_status_segments(
    segments: Sequence[tuple[str, str | None, str]],
    drop_classes: frozenset[str] = frozenset(),
) -> Text:
    kept = [s for s in segments if s[2] not in drop_classes]
    text = Text()
    for idx, seg in enumerate(kept):
        value, style = seg[0], seg[1]
        if idx:
            text.append(" ")
        text.append(value, style=style)
    return text


def _status_plain_text(report: BranchReport) -> str:
    if not report.status_segments:
        return _empty_status_text(report).plain
    return " ".join(seg[0] for seg in report.status_segments)


def _status_cell(report: BranchReport) -> ResponsiveCell:
    if not report.status_segments:
        return _responsive(_empty_status_text(report))

    v0 = _render_status_segments(report.status_segments)
    v1 = _render_status_segments(report.status_segments, frozenset({"plus_minus"}))
    v2 = _render_status_segments(
        report.status_segments, frozenset({"plus_minus", "extras"})
    )
    variants: list[Text] = [v0]
    for candidate in (v1, v2):
        if cell_len(candidate.plain) < cell_len(variants[-1].plain):
            variants.append(candidate)
    return ResponsiveCell(variants=tuple(variants))


def _slash_cell(value: str) -> ResponsiveCell:
    if value == "-":
        return _responsive("-")
    variants: list[Text] = [Text(value)]
    if "/" in value:
        tail = value.split("/", 1)[0]
        shortened = Text(f"{tail}/…")
        if cell_len(shortened.plain) < cell_len(variants[-1].plain):
            variants.append(shortened)
    return ResponsiveCell(variants=tuple(variants))


def _upstream_cell(report: BranchReport) -> ResponsiveCell:
    value = report.upstream
    if value == "-":
        return _responsive("-")

    remote_name = report.upstream_remote
    link_url = report.upstream_link_url
    remote_style = "cyan"
    if link_url:
        # Rich recognises "link <url>" inside a style string and emits OSC 8.
        remote_style = f"{remote_style} link {link_url}"

    if remote_name and value.startswith(f"{remote_name}/"):
        rest = value[len(remote_name) + 1:]
        full = Text()
        full.append(remote_name, style=remote_style)
        full.append("/")
        full.append(rest)
        variants: list[Text] = [full]
        short = Text()
        short.append(remote_name, style=remote_style)
        short.append("/…")
        if cell_len(short.plain) < cell_len(full.plain):
            variants.append(short)
        return ResponsiveCell(variants=tuple(variants))

    # Local-tracking (remote == ".") or unexpected format: no split, no link.
    return _slash_cell(value)


def _worktree_cell(value: str) -> ResponsiveCell:
    if value in {"-", "here"}:
        return _responsive(value)

    variants: list[Text] = [Text(value)]
    basename = os.path.basename(value.rstrip(os.sep))
    if basename and cell_len(basename) < cell_len(value):
        variants.append(Text(basename))
    return ResponsiveCell(variants=tuple(variants))


def _author_cell(value: str, fg: str | None) -> ResponsiveCell:
    style = fg or ""
    variants: list[Text] = [Text(value, style=style)]
    if " <" in value:
        author_name = value.split(" <", 1)[0]
        if cell_len(author_name) < cell_len(value):
            variants.append(Text(author_name, style=style))
    return ResponsiveCell(variants=tuple(variants))


def _branch_cell(report: BranchReport) -> ResponsiveCell:
    linked = report.worktree_display not in ("-", "here")
    name_style = "bold" if report.current else None

    if not linked:
        text = Text()
        text.append(report.branch, style=name_style)
        return _responsive(text)

    wt_style = "color(240)"
    if report.worktree_path is not None:
        wt_style = f"{wt_style} link file://{report.worktree_path}"

    full = Text()
    full.append(report.branch, style=name_style)
    full.append(" (WT)", style=wt_style)

    compact = Text()
    compact.append(report.branch, style=name_style)
    return ResponsiveCell(variants=(full, compact))


DEFAULT_COLUMNS = ["branch", "upstream", "status", "author", "subject", "date"]

_COLUMN_DEFS: dict[str, tuple[Callable[[], ResponsiveCell], int]] = {
    "branch": (lambda: _header_cell("Branch", "Br"), 500),
    "head": (lambda: _header_cell("Head"), 150),
    "upstream": (lambda: _header_cell("Upstream", "Up"), 350),
    "status": (lambda: _header_cell("Status"), 500),
    "ahead": (lambda: _header_cell("Ahead", "A"), 100),
    "behind": (lambda: _header_cell("Behind", "B"), 100),
    "gone": (lambda: _header_cell("Gone"), 100),
    "current": (lambda: _header_cell("Current", "Cur"), 100),
    "worktree": (lambda: _header_cell("Worktree", "WT"), 250),
    "date": (lambda: _header_cell("Date"), 1),
    "author": (lambda: _header_cell("Author", "Auth"), 150),
    "subject": (lambda: _header_cell("Subject", "Subj"), 400),
}


def _row_value(column: str, report: BranchReport) -> ResponsiveCell:
    if column == "branch":
        return _branch_cell(report)
    if column == "head":
        return _responsive(report.head)
    if column == "upstream":
        return _upstream_cell(report)
    if column == "status":
        return _status_cell(report)
    if column == "ahead":
        return _responsive(str(report.ahead))
    if column == "behind":
        return _responsive(str(report.behind))
    if column == "gone":
        return _bool_cell(report.gone)
    if column == "current":
        return _bool_cell(report.current)
    if column == "worktree":
        return _worktree_cell(report.worktree_display)
    if column == "date":
        return _date_cell(report.date)
    if column == "author":
        return _author_cell(report.author, report.author_fg)
    if column == "subject":
        return _responsive(report.subject or "-")
    raise ValueError(f"Unknown column: {column!r}")


__all__ = ["DEFAULT_COLUMNS", "parse_columns", "render_json", "render_table"]
