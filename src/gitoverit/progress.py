from __future__ import annotations

from enum import Enum
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


class HookReturnAction(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"


class ProgressHookProtocol(Protocol):
    def discovering(self, path: Path) -> HookReturnAction | None: ...

    def start_collect(self, total: int) -> HookReturnAction | None: ...

    def collecting(self, index: int, path: Path) -> HookReturnAction | None: ...

    def done(self) -> HookReturnAction | None: ...


class RichProgressHook(ProgressHookProtocol):
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

    def discovering(self, path: Path) -> HookReturnAction:
        self.discovered_count += 1
        if self.discovery_task_id is not None:
            description = (
                f"[cyan]Discovering repositories ({self.discovered_count})"
            )
            self.progress.update(self.discovery_task_id, description=description)
        return HookReturnAction.CONTINUE

    def start_collect(self, total: int) -> HookReturnAction:
        self.total_to_collect = total
        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        if total <= 0:
            return HookReturnAction.CONTINUE
        self.gather_task_id = self.progress.add_task(
            "[cyan]Gathering status...", total=total
        )
        return HookReturnAction.CONTINUE

    def collecting(self, index: int, path: Path) -> HookReturnAction:
        if self.gather_task_id is not None:
            display_name = path.name or str(path)
            description = (
                f"[cyan]Gathering status ({index}/{self.total_to_collect}): {display_name}"
            )
            self.progress.update(
                self.gather_task_id,
                advance=1,
                description=description,
            )
        return HookReturnAction.CONTINUE

    def done(self) -> HookReturnAction:
        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        self.progress.__exit__(None, None, None)
        return HookReturnAction.CONTINUE


def dispatch_hook(
    hook: ProgressHookProtocol | None, method: str, *args
) -> bool:
    if hook is None:
        return False
    handler = getattr(hook, method)
    result = handler(*args)
    return result is HookReturnAction.STOP


__all__ = [
    "HookReturnAction",
    "ProgressHookProtocol",
    "RichProgressHook",
    "dispatch_hook",
]
