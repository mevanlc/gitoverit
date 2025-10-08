from __future__ import annotations

from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)


class HookProtocol(Protocol):
    """Simple progress notification protocol - no flow control"""
    def discovering(self, path: Path) -> None: ...

    def start_collect(self, total: int) -> None: ...

    def collecting(self, index: int, path: Path) -> None: ...

    def done(self) -> None: ...


class RichHook(HookProtocol):
    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self.progress.__enter__()
        self.discovery_task_id: TaskID | None = self.progress.add_task(
            "[cyan]Discovering repositories...", total=None
        )
        self.discovered_count = 0
        self.gather_task_id: TaskID | None = None
        self.total_to_collect = 0
        self.error_count = 0  # Add error tracking for parallel mode

    def discovering(self, path: Path) -> None:
        self.discovered_count += 1
        if self.discovery_task_id is not None:
            description = (
                f"[cyan]Discovering repositories ({self.discovered_count})"
            )
            self.progress.update(self.discovery_task_id, description=description)

    def start_collect(self, total: int) -> None:
        self.total_to_collect = total
        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        if total <= 0:
            return
        self.gather_task_id = self.progress.add_task(
            "[cyan]Gathering status...", total=total
        )

    def collecting(self, index: int, path: Path) -> None:
        if self.gather_task_id is not None:
            display_name = path.name or str(path)
            error_text = f" [red]({self.error_count} errors)[/red]" if self.error_count > 0 else ""
            description = (
                f"[cyan]Gathering status ({index}/{self.total_to_collect}): {display_name}{error_text}"
            )
            self.progress.update(
                self.gather_task_id,
                advance=1,
                description=description,
            )

    def done(self) -> None:
        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        self.progress.__exit__(None, None, None)


__all__ = [
    "HookProtocol",
    "RichHook",
]
