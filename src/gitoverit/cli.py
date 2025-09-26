from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence
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
    NONE = "none"


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
        help="Sort repositories by mtime (default) or disable sorting with none",
    ),
) -> None:
    """Scan git repositories beneath the given directories and show their status."""

    reports: list[RepoReport] = []

    show_progress = _stdout_is_tty()
    progress: Progress | None = None
    discovery_task_id: int | None = None
    gather_task_id: int | None = None

    repo_paths: list[Path] = []

    try:
        if show_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            )
            progress.__enter__()
            discovery_task_id = progress.add_task("[cyan]Discovering repositories...", total=None)

        for repo_path in discover_repositories(dirs):
            repo_paths.append(repo_path)
            if progress is not None and discovery_task_id is not None:
                progress.update(
                    discovery_task_id,
                    description=f"[cyan]Discovering repositories ({len(repo_paths)})",
                )

        if progress is not None and discovery_task_id is not None:
            progress.remove_task(discovery_task_id)
            gather_task_id = progress.add_task(
                "[cyan]Gathering status...",
                total=len(repo_paths),
            )

        for index, repo_path in enumerate(repo_paths, start=1):
            if progress is not None and gather_task_id is not None:
                display_name = repo_path.name or str(repo_path)
                progress.update(
                    gather_task_id,
                    description=(
                        f"[cyan]Gathering status ({index}/{len(repo_paths)}): "
                        f"{display_name}"
                    ),
                )
            report = analyze_repository(repo_path, fetch=fetch)
            if progress is not None and gather_task_id is not None:
                progress.update(gather_task_id, advance=1)
            if dirty_only and not report.dirty and not report.fetch_failed:
                continue
            reports.append(report)
    finally:
        if progress is not None:
            progress.__exit__(None, None, None)

    if sort is SortMode.MTIME:
        reports.sort(key=lambda report: report.latest_mtime or 0.0, reverse=True)

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
        fetch_url = config.get_value(section, "url")
    except Exception:
        return None
    push_url = None
    try:
        push_url = config.get_value(section, "pushurl")
    except Exception:
        push_url = None
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
    worktree = Path(repo.working_tree_dir).resolve()
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
