from __future__ import annotations

from typing import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..reporting import RepoReport


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


__all__ = ["render_table"]
