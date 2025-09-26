from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Protocol, Sequence
from urllib.parse import urlparse

import typer
from git import GitCommandError, Repo
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

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
        self.discovery_task_id = self.progress.add_task(
            "[cyan]Discovering repositories...", total=None
        )
        self.discovered_count = 0
        self.gather_task_id: int | None = None
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


def _hook_requests_stop(
    hook: ProgressHookProtocol | None, method: str, *args
) -> bool:
    if hook is None:
        return False
    handler = getattr(hook, method)
    result = handler(*args)
    return result is HookReturnAction.STOP


@dataclass
class RepoReport:
    path: Path
    display_path: str
    fetch_failed: bool
    status_segments: Sequence[tuple[str, str | None]]
    branch: str
    remote: str
    remote_url: str
    ident: str | None
    dirty: bool
    latest_mtime: float | None

    def status_text(self) -> Text:
        if not self.status_segments:
            return Text("clean", style="green")
        text = Text()
        for idx, (value, style) in enumerate(self.status_segments):
            if idx:
                text.append(" ")
            text.append(value, style=style)
        return text


EXCEPTION_SENTINELS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
)


def _default_sentinel_checker(repo: Repo, git_dir: Path) -> bool:
    return True


def _rev_parse_exists(repo: Repo, ref: str) -> bool:
    try:
        repo.git.rev_parse(ref)
    except GitCommandError:
        return False
    return True


def _rebase_metadata_present(git_dir: Path) -> bool:
    return (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir()


def _sequencer_active(git_dir: Path) -> bool:
    sequencer = git_dir / "sequencer"
    if not sequencer.is_dir():
        return False
    todo = sequencer / "todo"
    return todo.exists() and todo.stat().st_size > 0


def _bisect_active(git_dir: Path) -> bool:
    return (git_dir / "BISECT_START").exists()


SENTINEL_VALIDATORS: dict[str, Callable[[Repo, Path], bool]] = {
    "MERGE_HEAD": lambda repo, git_dir: _rev_parse_exists(repo, "MERGE_HEAD"),
    "REBASE_HEAD": lambda repo, git_dir: _rebase_metadata_present(git_dir),
    "rebase-merge": lambda repo, git_dir: _rebase_metadata_present(git_dir),
    "rebase-apply": lambda repo, git_dir: _rebase_metadata_present(git_dir),
    "CHERRY_PICK_HEAD": lambda repo, git_dir: _sequencer_active(git_dir)
    and _rev_parse_exists(repo, "CHERRY_PICK_HEAD"),
    "REVERT_HEAD": lambda repo, git_dir: _sequencer_active(git_dir)
    and _rev_parse_exists(repo, "REVERT_HEAD"),
    "BISECT_LOG": lambda repo, git_dir: _bisect_active(git_dir),
}


def unescalate_sentinel_file_exists(repo: Repo, sentinel: str) -> bool:
    checker = SENTINEL_VALIDATORS.get(sentinel, _default_sentinel_checker)
    return checker(repo, Path(repo.git_dir))


@APP.command()
def cli(
    dirs: List[Path] = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, writable=False
    ),
    fetch: bool = typer.Option(
        False, "--fetch", help="Run git fetch --all for each repository before inspection."
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.TABLE, "--format", case_sensitive=False, help="Choose output format."
    ),
    dirty_only: bool = typer.Option(
        False, "--dirty-only", help="Display only repositories with local or remote changes."
    ),
    sort: SortMode = typer.Option(
        SortMode.MTIME,
        "--sort",
        case_sensitive=False,
        help="Sort repositories by mtime (default), author, or disable sorting with none",
    ),
    reverse: bool = typer.Option(
        False,
        "--reverse",
        help="Reverse sort order when a sort mode is active.",
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    reports: list[RepoReport] = []
    repo_paths: list[Path] = []

    hook: ProgressHookProtocol | None = None
    stop_requested = False

    try:
        if _stdout_is_tty():
            hook = RichProgressHook(console)

        for repo_path in discover_repositories(dirs):
            if _hook_requests_stop(hook, "discovering", repo_path):
                stop_requested = True
                break
            repo_paths.append(repo_path)

        if not stop_requested and repo_paths:
            if _hook_requests_stop(hook, "start_collect", len(repo_paths)):
                stop_requested = True
            else:
                for index, repo_path in enumerate(repo_paths, start=1):
                    report = analyze_repository(repo_path, fetch=fetch)
                    include = not (
                        dirty_only and not report.dirty and not report.fetch_failed
                    )
                    if include:
                        reports.append(report)
                    if _hook_requests_stop(hook, "collecting", index, repo_path):
                        stop_requested = True
                        break
    finally:
        if hook is not None:
            hook.done()

    if sort is SortMode.MTIME:
        reports.sort(key=lambda report: report.latest_mtime or 0.0, reverse=not reverse)
    elif sort is SortMode.AUTHOR:
        reports.sort(
            key=lambda report: (report.ident or "").lower(),
            reverse=reverse,
        )
    elif reverse:
        reports.reverse()

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps([report_to_dict(r) for r in reports], indent=2, sort_keys=True))
    else:
        render_table(reports)


def discover_repositories(roots: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    normalized_roots = [root.resolve() for root in roots]
    for root in normalized_roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root):
            current = Path(dirpath)
            git_dir = current / ".git"
            if git_dir.exists():
                if is_submodule_gitdir(git_dir):
                    dirnames[:] = []
                    continue
                resolved = current.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield resolved
            if ".git" in dirnames:
                dirnames.remove(".git")


def is_submodule_gitdir(git_dir: Path) -> bool:
    if git_dir.is_dir():
        return False
    try:
        content = git_dir.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "modules" in content


def analyze_repository(path: Path, fetch: bool) -> RepoReport:
    repo = Repo(path)
    fetch_failed = False

    if fetch and repo.remotes:
        for remote in repo.remotes:
            try:
                remote.fetch()
            except GitCommandError:
                fetch_failed = True
                break

    status = repo.git.status("--porcelain")
    parsed = parse_status_porcelain(status)

    additions, deletions = diff_numstat_totals(repo)

    ahead, behind, remote_ref, remote_url = compute_branch_tracking(repo)
    branch_name = determine_branch(repo)

    exceptional = has_exceptional_state(repo, parsed)
    submodule_count = count_submodules(repo)

    ident = read_git_ident(repo)
    latest_mtime = latest_worktree_mtime(repo)

    segments: list[tuple[str, str | None]] = []
    if parsed.modified_count:
        segments.append((f"{parsed.modified_count}m", "yellow"))
    if additions or deletions:
        segments.append((f"(+{additions}/-{deletions})", "cyan"))
    if parsed.untracked_count:
        segments.append((f"{parsed.untracked_count}u", "magenta"))
    if parsed.deleted_count:
        segments.append((f"{parsed.deleted_count}d", "red"))
    if submodule_count:
        segments.append((f"{submodule_count}s", "blue"))
    if ahead:
        segments.append((f"{ahead}\u2191", "green"))
    if behind:
        segments.append((f"{behind}\u2193", "bright_black"))
    if exceptional:
        segments.append(("!", "bold red"))

    dirty = bool(
        parsed.modified_count
        or additions
        or deletions
        or parsed.untracked_count
        or parsed.deleted_count
        or ahead
        or behind
        or exceptional
    )

    display_path = relativize(path)
    if fetch_failed:
        display_path = f"! {display_path}"

    return RepoReport(
        path=path,
        display_path=display_path,
        fetch_failed=fetch_failed,
        status_segments=segments,
        branch=branch_name,
        remote=remote_ref or "-",
        remote_url=remote_url or "-",
        ident=ident,
        dirty=dirty,
        latest_mtime=latest_mtime,
    )


@dataclass
class ParsedStatus:
    modified_count: int
    untracked_count: int
    deleted_count: int
    has_conflicts: bool


def parse_status_porcelain(output: str) -> ParsedStatus:
    modified_paths: set[str] = set()
    deleted_paths: set[str] = set()
    untracked = 0
    has_conflicts = False
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        if code == "??":
            untracked += 1
            continue
        index_status, worktree_status = code
        if index_status == "U" or worktree_status == "U" or code in {"AA", "DD"}:
            has_conflicts = True
        if index_status == "D" or worktree_status == "D":
            deleted_paths.add(raw_line[3:])
        if any(status in {"M", "A", "R", "C"} for status in (index_status, worktree_status)):
            modified_paths.add(raw_line[3:])
    return ParsedStatus(
        modified_count=len(modified_paths),
        untracked_count=untracked,
        deleted_count=len(deleted_paths),
        has_conflicts=has_conflicts,
    )


def diff_numstat_totals(repo: Repo) -> tuple[int, int]:
    added = 0
    removed = 0
    for args in (("--numstat", "--cached"), ("--numstat",)):
        try:
            output = repo.git.diff(*args)
        except GitCommandError:
            continue
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_str, del_str = parts[0], parts[1]
            adds = int(add_str) if add_str.isdigit() else 0
            dels = int(del_str) if del_str.isdigit() else 0
            added += adds
            removed += dels
    return added, removed


def compute_branch_tracking(repo: Repo) -> tuple[int, int, str | None, str | None]:
    if repo.head.is_detached:
        return 0, 0, None, None
    try:
        branch = repo.active_branch
    except (TypeError, GitCommandError):
        return 0, 0, None, None
    tracking = branch.tracking_branch()
    if tracking is None:
        return 0, 0, None, None
    ahead = behind = 0
    try:
        counts = repo.git.rev_list("--left-right", "--count", f"{branch.name}...{tracking.name}")
        left, right = counts.strip().split()
        ahead = int(left)
        behind = int(right)
    except (GitCommandError, ValueError):
        pass
    remote_ref = f"{tracking.remote_name}/{tracking.remote_head}"
    remote_url = format_remote_urls(repo, tracking.remote_name)
    return ahead, behind, remote_ref, remote_url


def format_remote_urls(repo: Repo, remote_name: str) -> str | None:
    config = repo.config_reader()
    section = f"remote \"{remote_name}\""
    try:
        fetch_value = config.get_value(section, "url")
    except Exception:
        return None
    if isinstance(fetch_value, str):
        fetch_url = fetch_value
    else:
        fetch_url = str(fetch_value)
    push_url = None
    try:
        push_value = config.get_value(section, "pushurl")
    except Exception:
        push_value = None
    if isinstance(push_value, str) or push_value is None:
        push_url = push_value
    else:
        push_url = str(push_value)
    formatted_fetch = simplify_url(fetch_url)
    if not push_url or push_url == fetch_url:
        return formatted_fetch
    formatted_push = simplify_url(push_url)
    return f"{formatted_fetch}\n{formatted_push}"


def simplify_url(url: str) -> str:
    original_url = url

    scp_match = re.match(r"^(?P<user>[^@]+)@(?P<host>[^:]+):(?P<path>.+)$", url)
    if scp_match:
        host = scp_match.group("host")
        path = scp_match.group("path")
        is_ssh = True
    else:
        parsed = urlparse(url)
        if not parsed.scheme and re.match(r"^[^@]+@[^:]+:.+", url):
            # Fallback for scp syntax missing scheme
            scp_match = re.match(r"^(?P<user>[^@]+)@(?P<host>[^:]+):(?P<path>.+)$", url)
            if scp_match:
                host = scp_match.group("host")
                path = scp_match.group("path")
                is_ssh = True
            else:
                host = parsed.hostname or ""
                path = parsed.path or ""
                is_ssh = False
        else:
            host = parsed.hostname or ""
            path = parsed.path or ""
            is_ssh = parsed.scheme in {"ssh", "git+ssh"}
        if parsed.scheme == "ssh":
            is_ssh = True

    path = path.lstrip("/").rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]

    service_prefix_map = {
        "github.com": "",
        "gitlab.com": "gl:",
        "bitbucket.org": "bb:",
        "codeberg.org": "cb:",
    }
    prefix = service_prefix_map.get(host)
    if prefix is None:
        prefix = f"{host}/" if host else ""

    if prefix.endswith(":"):
        display = f"{prefix}{path}"
    else:
        display = path if prefix == "" else (f"{prefix}{path}" if path else prefix.rstrip("/"))

    if is_ssh and display:
        display = f"ssh+{display}"

    if not display:
        display = original_url

    return display


def determine_branch(repo: Repo) -> str:
    if repo.head.is_detached:
        commit = repo.head.commit.hexsha[:7]
        return f"DETACHED@{commit}"
    try:
        return repo.active_branch.name
    except (TypeError, GitCommandError):
        return "UNKNOWN"


def has_exceptional_state(repo: Repo, parsed: ParsedStatus) -> bool:
    if parsed.has_conflicts:
        return True
    if repo.head.is_detached:
        return True
    git_dir = Path(repo.git_dir)
    for sentinel in EXCEPTION_SENTINELS:
        if (git_dir / sentinel).exists():
            if unescalate_sentinel_file_exists(repo, sentinel):
                return True
    return False


def count_submodules(repo: Repo) -> int:
    try:
        return len(repo.submodules)
    except Exception:
        return 0


IDENT_PATTERN = re.compile(r"\s+\d+\s+[+-]\d+$")


def read_git_ident(repo: Repo) -> str | None:
    try:
        ident = repo.git.var("GIT_COMMITTER_IDENT").strip()
    except GitCommandError:
        return None
    return IDENT_PATTERN.sub("", ident)


def relativize(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def render_table(reports: Sequence[RepoReport]) -> None:
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
            report.status_text(),
            report.branch,
            report.remote,
            report.remote_url,
            report.ident or "-",
        )
    console.print(table)


def report_to_dict(report: RepoReport) -> dict[str, object]:
    status = " ".join(segment for segment, _ in report.status_segments)
    if not status:
        status = "clean"
    return {
        "path": str(report.path),
        "display_path": report.display_path,
        "fetch_failed": report.fetch_failed,
        "status": status,
        "branch": report.branch,
        "remote": report.remote,
        "remote_url": report.remote_url,
        "ident": report.ident,
        "dirty": report.dirty,
        "latest_mtime": report.latest_mtime,
    }


def latest_worktree_mtime(repo: Repo) -> float | None:
    worktree_dir = repo.working_tree_dir
    if worktree_dir is None:
        return None
    worktree = Path(worktree_dir).resolve()
    candidates: set[Path] = {worktree}

    def _add_path(rel_path: str) -> None:
        if not rel_path:
            return
        if rel_path.startswith(".git/") or rel_path == ".git":
            return
        absolute = (worktree / rel_path).resolve()
        if not absolute.exists():
            return
        try:
            if absolute != worktree and not absolute.is_relative_to(worktree):
                return
        except AttributeError:
            if absolute != worktree and worktree not in absolute.parents:
                return
        if absolute.is_dir():
            candidates.add(absolute)
        else:
            candidates.add(absolute)
        try:
            if absolute.is_relative_to(worktree):
                for parent in absolute.parents:
                    if parent == worktree or parent.is_relative_to(worktree):
                        candidates.add(parent)
                    else:
                        break
                    if parent == worktree:
                        break
        except AttributeError:
            # Python <3.9 fallback (shouldn't happen under >=3.11, but guard anyway)
            current = absolute
            while worktree in current.parents:
                candidates.add(current.parent)
                if current.parent == worktree:
                    break
                current = current.parent

    try:
        tracked = repo.git.ls_files().splitlines()
    except GitCommandError:
        tracked = []
    for rel_path in tracked:
        _add_path(rel_path)

    try:
        untracked = repo.git.ls_files("--others", "--exclude-standard").splitlines()
    except GitCommandError:
        untracked = []
    for rel_path in untracked:
        _add_path(rel_path)

    latest: float | None = None
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except FileNotFoundError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def _stdout_is_tty() -> bool:
    try:
        return os.isatty(sys.stdout.fileno())
    except Exception:
        return False


def main() -> None:
    APP()


__all__ = ["APP", "main"]
