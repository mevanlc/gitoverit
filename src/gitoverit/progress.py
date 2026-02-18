from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from traceback import TracebackException
from typing import IO, Protocol

from rich.console import Console


def _get_debug_log() -> IO[str] | None:
    """Open debug log file if GITOVERIT_DEBUG_LOG is set."""
    log_path = os.environ.get("GITOVERIT_DEBUG_LOG")
    if not log_path:
        return None
    try:
        return open(log_path, "a", buffering=1)  # line-buffered
    except OSError:
        return None


def _log(log: IO[str] | None, event: str, **kwargs: object) -> None:
    """Write a timestamped log entry."""
    if log is None:
        return
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    details = " ".join(f"{k}={v}" for k, v in kwargs.items())
    log.write(f"{ts} {event} {details}\n")
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

    def error(self, path: Path, tb: TracebackException | None = None) -> None: ...

    def done(self) -> None: ...

    def get_errors(self) -> list[tuple[Path, TracebackException | None]]: ...


class SilentHook:
    """Hook that tracks errors without progress display (for non-TTY)."""

    def __init__(self) -> None:
        self._log = _get_debug_log()
        _log(self._log, "init", mode="silent")
        self.errors: list[tuple[Path, TracebackException | None]] = []

    def discovering(self, path: Path) -> None:
        _log(self._log, "discovering", path=path)

    def discovery_done(self) -> None:
        _log(self._log, "discovery_done")

    def start_collect(self, total: int) -> None:
        _log(self._log, "start_collect", total=total)

    def collecting(self, index: int, path: Path) -> None:
        _log(self._log, "collecting", index=index, path=path, errors=len(self.errors))

    def error(self, path: Path, tb: TracebackException | None = None) -> None:
        exc_info = ""
        if tb is not None:
            exc_type = tb.exc_type.__name__ if tb.exc_type else "?"
            exc_msg = str(tb).split("\n")[-1].strip()
            exc_info = f"{exc_type}: {exc_msg}"
        _log(self._log, "error", path=path, exc=exc_info)
        self.errors.append((path, tb))

    def get_errors(self) -> list[tuple[Path, TracebackException | None]]:
        return self.errors

    def done(self) -> None:
        _log(self._log, "done", errors=len(self.errors))
        if self._log is not None:
            self._log.close()
            self._log = None


class RichHook(HookProtocol):
    def __init__(self, console: Console) -> None:
        self.console = console
        self._closed = False
        self._log = _get_debug_log()
        _log(self._log, "init")
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
        self.error_count = 0
        self.errors: list[tuple[Path, TracebackException | None]] = []

    def discovering(self, path: Path) -> None:
        _log(self._log, "discovering", path=path)
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
        _log(self._log, "discovery_done", count=self.discovered_count)
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
        _log(self._log, "start_collect", total=total)
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
        _log(self._log, "collecting", index=index, path=path, errors=self.error_count)
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

    def error(self, path: Path, tb: TracebackException | None = None) -> None:
        exc_info = ""
        if tb is not None:
            exc_type = tb.exc_type.__name__ if tb.exc_type else "?"
            exc_msg = str(tb).split("\n")[-1].strip()
            exc_info = f"{exc_type}: {exc_msg}"
        _log(self._log, "error", path=path, exc=exc_info)
        self.error_count += 1
        self.errors.append((path, tb))

    def get_errors(self) -> list[tuple[Path, TracebackException | None]]:
        return self.errors

    def done(self) -> None:
        if self._closed:
            return
        self._closed = True

        _log(self._log, "done", errors=self.error_count)
        if self._log is not None:
            self._log.close()
            self._log = None

        if self.discovery_task_id is not None:
            self.progress.remove_task(self.discovery_task_id)
            self.discovery_task_id = None
        if self.status_task_id is not None:
            self.progress.remove_task(self.status_task_id)
            self.status_task_id = None
        self.progress.__exit__(None, None, None)


__all__ = [
    "HookProtocol",
    "RichHook",
    "SilentHook",
]
