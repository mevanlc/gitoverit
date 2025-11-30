from __future__ import annotations

from typing import Sequence

import re
import rich._wrap as rich_wrap
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..reporting import RepoReport


rich_wrap.re_word = re.compile(r"[^\S\u00A0]*[\S\u00A0]+[^\S\u00A0]*")

NBSP = "\u00A0"


def _status_key_main() -> Text:
    text = Text("  Status key: ")
    first = True

    def add(symbol: str, description: str, style: str | None) -> None:
        nonlocal first
        if not first:
            text.append("  ")
        if style:
            text.append(symbol, style=style)
        else:
            text.append(symbol)
        text.append(NBSP)
        text.append(description)
        first = False

    add("m", "modified", "yellow")
    add("u", "untracked", "magenta")
    add("d", "deleted", "red")
    add("+/-", f"lines{NBSP}added/removed", "cyan")
    add("↑", "ahead", "green")
    add("↓", "behind", "bright_black")
    add("s", "submodules", "blue")

    return text


def _status_key_exceptional() -> Text:
    text = Text("              ")
    text.append("!", style="bold red")
    text.append(" ")
    text.append(
        "any of: conflicts, detached HEAD, in-progress/unfinished operation "
        "(merge, rebase, cherry-pick, etc.)"
    )
    return text


def _status_text(report: RepoReport) -> Text:
    if not report.status_segments:
        return Text("clean", style="green")
    text = Text()
    for idx, (value, style) in enumerate(report.status_segments):
        if idx:
            text.append(" ")
        text.append(value, style=style)
    return text


def render_table(console: Console, reports: Sequence[RepoReport]) -> None:
    show_exceptional_key = any(
        any(segment == "!" for segment, _ in report.status_segments) for report in reports
    )

    table = Table()
    table.add_column("Dir")
    table.add_column("Status")
    table.add_column("Branch")
    table.add_column("Remote")
    table.add_column("URL")
    table.add_column("Ident")

    for report in reports:
        table.add_row(
            report.display_path,
            _status_text(report),
            report.branch,
            report.remote,
            report.remote_url,
            report.ident or "-",
        )
    console.print(table)

    if reports:
        console.print(_status_key_main())
        if show_exceptional_key:
            console.print(_status_key_exceptional())


__all__ = ["render_table"]
