from __future__ import annotations

from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)


class HookProtocol(Protocol):
    """Simple progress notification protocol - no flow control"""
    def discovering(self, path: Path) -> None: ...

    def discovery_done(self) -> None: ...

    def start_collect(self, total: int) -> None: ...

    def collecting(self, index: int, path: Path) -> None: ...

    def error(self, path: Path) -> None: ...

    def done(self) -> None: ...


class RichHook(HookProtocol):
    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self.progress.__enter__()
        self.discovery_task_id: TaskID | None = self.progress.add_task(
            "[cyan]Repo discovery", total=None
        )
        self.status_task_id: TaskID | None = self.progress.add_task(
            "[cyan]Repo statusing", total=1, visible=False
        )
        self.discovered_count = 0
        self.discovery_finished = False
        self.total_to_collect = 0
        self.error_count = 0  # Add error tracking for parallel mode

    def discovering(self, path: Path) -> None:
        if self.discovery_finished:
            return
        self.discovered_count += 1
        if self.discovery_task_id is not None:
            description = (
                f"[cyan]Repo discovery ({self.discovered_count})"
            )
            self.progress.update(self.discovery_task_id, description=description)

        if self.status_task_id is not None:
            self.progress.update(
                self.status_task_id,
                total=self.discovered_count,
                visible=True,
            )

    def discovery_done(self) -> None:
        self.discovery_finished = True
        if self.discovery_task_id is None:
            return

        if self.discovered_count <= 0:
            # No repos discovered; avoid a misleading 1/1 style display.
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
            return

        self.progress.update(
            self.discovery_task_id,
            total=self.discovered_count,
            completed=self.discovered_count,
            description=f"[green]Repo discovery done ({self.discovered_count})",
        )

    def start_collect(self, total: int) -> None:
        effective_total = max(total, self.discovered_count)
        self.total_to_collect = effective_total

        if self.status_task_id is None:
            self.status_task_id = self.progress.add_task(
                "[cyan]Repo statusing", total=max(effective_total, 1), visible=effective_total > 0
            )
            return

        if effective_total > 0:
            self.progress.update(
                self.status_task_id,
                total=effective_total,
                visible=True,
            )

    def collecting(self, index: int, path: Path) -> None:
        if self.status_task_id is None:
            return

        effective_total = max(self.total_to_collect, self.discovered_count)
        self.total_to_collect = effective_total

        display_name = path.name or str(path)
        error_text = (
            f" [red]({self.error_count} errors)[/red]" if self.error_count > 0 else ""
        )
        description = f"[cyan]Repo statusing: {display_name}{error_text}"
        self.progress.update(
            self.status_task_id,
            total=max(effective_total, 1),
            completed=index,
            visible=effective_total > 0,
            description=description,
        )

    def error(self, path: Path) -> None:
        self.error_count += 1

    def done(self) -> None:
        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        self.progress.__exit__(None, None, None)


__all__ = [
    "HookProtocol",
    "RichHook",
]
