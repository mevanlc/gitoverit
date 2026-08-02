from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from traceback import TracebackException
from typing import Callable, Iterable, Iterator, Sequence
from urllib.parse import urlparse

from git import GitCommandError, Repo
from rich.text import Text

from .progress import HookProtocol


@dataclass
class RepoReport:
    path: Path
    display_path: str
    fetch_failed: bool
    # (text, style, narrow_class) — narrow_class ∈ {"core", "extras", "plus_minus"}
    # and tells the Status cell which parts to drop first when narrowing.
    status_segments: Sequence[tuple[str, str | None, str]]
    branch: str
    remote: str
    remote_url: str
    ident: str | None
    dirty: bool | None
    latest_mtime: float | None
    ahead: int = 0
    behind: int = 0
    modified: int | None = 0
    untracked: int | None = 0
    deleted: int | None = 0
    pull_failed: bool = False
    pulled: bool = False
    worktree_status_checked: bool = True
    push_failed: bool = False
    pushed: bool = False

    def status_text(self) -> Text:
        if not self.status_segments:
            return Text("clean", style="green")
        text = Text()
        for idx, segment in enumerate(self.status_segments):
            value, style = segment[0], segment[1]
            if idx:
                text.append(" ")
            text.append(value, style=style)
        return text


@dataclass
class ParsedStatus:
    modified_count: int
    untracked_count: int
    deleted_count: int
    has_conflicts: bool
    has_tracked_changes: bool = False


@dataclass
class StatusSnapshot:
    parsed: ParsedStatus
    branch_oid: str | None
    branch_head: str | None
    upstream: str | None
    ahead: int
    behind: int


@dataclass
class RepoState:
    parsed: ParsedStatus
    additions: int
    deletions: int
    ahead: int
    behind: int
    remote_ref: str | None
    remote_url: str | None
    branch_name: str
    exceptional: bool
    submodule_count: int
    ident: str | None
    latest_mtime: float | None
    dirty: bool
    worktree_status_checked: bool = True


EXCEPTION_SENTINELS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
)


def collect_reports(
    dirs: Iterable[Path],
    *,
    fetch: bool,
    dirty_only: bool,
    include_ignored: bool = False,
    pull_safe: bool = False,
    push_safe: bool = False,
    metadata_only: bool = False,
    collect_mtime: bool = True,
    hook: HookProtocol | None = None,
) -> list[RepoReport]:
    # Backwards-compatible wrapper for sequential runs.
    return collect_reports_parallel(
        dirs,
        fetch=fetch,
        dirty_only=dirty_only,
        include_ignored=include_ignored,
        pull_safe=pull_safe,
        push_safe=push_safe,
        metadata_only=metadata_only,
        collect_mtime=collect_mtime,
        hook=hook,
        max_workers=0,
    )


def get_worker_count(user_override: int | None = None) -> int:
    """Simple worker count logic"""
    if user_override is not None:
        return max(0, user_override)

    cpu_count = os.cpu_count() or 1
    if cpu_count <= 2:
        return cpu_count
    else:
        return min(cpu_count - 1, 8)


def collect_reports_parallel(
    dirs: Iterable[Path],
    *,
    fetch: bool,
    dirty_only: bool,
    include_ignored: bool = False,
    pull_safe: bool = False,
    push_safe: bool = False,
    metadata_only: bool = False,
    collect_mtime: bool = True,
    hook: HookProtocol | None = None,
    max_workers: int | None = None,
) -> list[RepoReport]:
    """Parallel version of collect_reports with streaming discovery"""

    reports: list[RepoReport] = []
    discovered_total = 0
    statused_total = 0

    try:
        if hook:
            hook.start_collect(0)

        worker_count = get_worker_count(max_workers)
        if worker_count == 0:
            repo_paths: list[Path] = []
            for repo_path in discover_repositories(
                dirs,
                include_ignored=include_ignored,
            ):
                discovered_total += 1
                if hook:
                    hook.discovering(repo_path)
                repo_paths.append(repo_path)

            if hook:
                hook.discovery_done()
                hook.start_collect(discovered_total)

            for index, repo_path in enumerate(repo_paths, start=1):
                statused_total = index
                try:
                    report = analyze_repository(
                        repo_path,
                        fetch=fetch,
                        pull_safe=pull_safe,
                        push_safe=push_safe,
                        metadata_only=metadata_only,
                        collect_mtime=collect_mtime,
                    )
                    if not (
                        dirty_only
                        and not report.dirty
                        and not report.fetch_failed
                        and not report.pull_failed
                        and not report.push_failed
                    ):
                        reports.append(report)
                except Exception as exc:
                    if hook:
                        tb = TracebackException.from_exception(exc)
                        hook.error(repo_path, tb)
                if hook:
                    hook.collecting(index, repo_path)

            return reports

        max_pending = max(1, worker_count * 4)

        discovery_finished = False
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            repo_iter = iter(
                discover_repositories(
                    dirs,
                    include_ignored=include_ignored,
                )
            )
            futures: dict[Future[RepoReport], Path] = {}

            while True:
                while not discovery_finished and len(futures) < max_pending:
                    try:
                        repo_path = next(repo_iter)
                    except StopIteration:
                        discovery_finished = True
                        if hook:
                            hook.discovery_done()
                            hook.start_collect(discovered_total)
                        break

                    discovered_total += 1
                    if hook:
                        hook.discovering(repo_path)

                    future = executor.submit(
                        analyze_repository,
                        repo_path,
                        fetch,
                        pull_safe,
                        metadata_only,
                        push_safe,
                        collect_mtime,
                    )
                    futures[future] = repo_path

                if not futures:
                    if discovery_finished:
                        break
                    continue

                completed, _ = wait(
                    futures,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    path = futures.pop(future)
                    statused_total += 1
                    try:
                        report = future.result()
                        if not (
                            dirty_only
                            and not report.dirty
                            and not report.fetch_failed
                            and not report.pull_failed
                            and not report.push_failed
                        ):
                            reports.append(report)
                    except Exception as exc:
                        if hook:
                            tb = TracebackException.from_exception(exc)
                            hook.error(path, tb)
                    if hook:
                        hook.collecting(statused_total, path)
    finally:
        if hook:
            hook.done()

    return reports


def discover_repositories(
    roots: Iterable[Path],
    *,
    include_ignored: bool = False,
) -> Iterator[Path]:
    seen: set[Path] = set()
    known_repos: list[Path] = []
    ignored_directories: set[Path] = set()
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
                    if not include_ignored and _is_gitignored_by_parent(
                        resolved,
                        known_repos,
                    ):
                        dirnames[:] = []
                        continue
                    seen.add(resolved)
                    known_repos.append(resolved)
                    yield resolved
                    if not include_ignored:
                        ignored_directories.update(_gitignored_directories(resolved))
            if ".git" in dirnames:
                dirnames.remove(".git")
            if ignored_directories:
                dirnames[:] = [
                    name
                    for name in dirnames
                    if current / name not in ignored_directories
                ]


def _gitignored_directories(repo_path: Path) -> set[Path]:
    """Return ignored directory roots that discovery may safely prune."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "--no-empty-directory",
            "-z",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        return set()
    return {
        repo_path / os.fsdecode(raw_path).removesuffix("/")
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    }


def is_submodule_gitdir(git_dir: Path) -> bool:
    if git_dir.is_dir():
        return False
    try:
        content = git_dir.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "modules" in content


def _nearest_parent_repo(path: Path, known_repos: list[Path]) -> Path | None:
    nearest: Path | None = None
    nearest_depth = 0
    for repo in known_repos:
        if repo == path:
            continue
        try:
            path.relative_to(repo)
        except ValueError:
            continue
        depth = len(repo.parts)
        if depth > nearest_depth:
            nearest = repo
            nearest_depth = depth
    return nearest


def _is_gitignored_by_parent(path: Path, known_repos: list[Path]) -> bool:
    parent = _nearest_parent_repo(path, known_repos)
    if parent is None:
        return False
    rel = path.relative_to(parent)
    result = subprocess.run(
        ["git", "-C", str(parent), "check-ignore", "-q", str(rel)],
        capture_output=True,
    )
    return result.returncode == 0


def analyze_repository(
    path: Path,
    fetch: bool,
    pull_safe: bool = False,
    metadata_only: bool = False,
    push_safe: bool = False,
    collect_mtime: bool = True,
) -> RepoReport:
    repo = Repo(path)
    fetch_failed = False
    pull_failed = False
    pulled = False
    push_failed = False
    pushed = False

    if (fetch or pull_safe or push_safe) and repo.remotes:
        if not _run_git_operation(path, "fetch", "--all", timeout=60):
            fetch_failed = True

    state = _collect_repo_state(
        repo,
        metadata_only=metadata_only,
        collect_mtime=collect_mtime,
    )

    if pull_safe and _can_pull_safely(state, fetch_failed=fetch_failed):
        if _run_git_operation(path, "pull", "--ff-only", timeout=120):
            pulled = True
        else:
            pull_failed = True
        state = _collect_repo_state(
            repo,
            metadata_only=metadata_only,
            collect_mtime=collect_mtime,
        )

    push_target = _upstream_push_target(repo) if push_safe else None
    if (
        push_safe
        and push_target is not None
        and _can_push_safely(state, fetch_failed=fetch_failed)
    ):
        remote_name, remote_branch = push_target
        refspec = f"HEAD:refs/heads/{remote_branch}"
        if _run_git_operation(path, "push", remote_name, refspec, timeout=120):
            pushed = True
        else:
            push_failed = True
        state = _collect_repo_state(
            repo,
            metadata_only=metadata_only,
            collect_mtime=collect_mtime,
        )

    segments = _render_repo_state_segments(state, pulled=pulled, pushed=pushed)

    display_path = relativize(path)
    if fetch_failed or pull_failed or push_failed:
        display_path = f"! {display_path}"

    return RepoReport(
        path=path,
        display_path=display_path,
        fetch_failed=fetch_failed,
        pull_failed=pull_failed,
        pulled=pulled,
        push_failed=push_failed,
        pushed=pushed,
        status_segments=segments,
        branch=state.branch_name,
        remote=state.remote_ref or "-",
        remote_url=state.remote_url or "-",
        ident=state.ident,
        dirty=state.dirty if state.worktree_status_checked else None,
        latest_mtime=state.latest_mtime,
        ahead=state.ahead,
        behind=state.behind,
        modified=(state.parsed.modified_count if state.worktree_status_checked else None),
        untracked=(state.parsed.untracked_count if state.worktree_status_checked else None),
        deleted=(state.parsed.deleted_count if state.worktree_status_checked else None),
        worktree_status_checked=state.worktree_status_checked,
    )


def _run_git_operation(path: Path, *args: str, timeout: int) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _collect_repo_state(
    repo: Repo,
    *,
    metadata_only: bool = False,
    collect_mtime: bool = True,
) -> RepoState:
    if metadata_only:
        parsed = ParsedStatus(0, 0, 0, False)
        additions = deletions = 0
        latest_mtime = None
        ahead, behind, remote_ref, remote_url = compute_branch_tracking(repo)
        branch_name = determine_branch(repo)
    else:
        snapshot = collect_status_snapshot(repo)
        parsed = snapshot.parsed
        if parsed.has_tracked_changes:
            additions, deletions = diff_numstat_totals(repo)
        else:
            additions = deletions = 0
        latest_mtime = latest_worktree_mtime(repo) if collect_mtime else None
        ahead = snapshot.ahead
        behind = snapshot.behind
        remote_ref, remote_url = tracking_display(repo, snapshot.upstream)
        branch_name = snapshot_branch_name(snapshot)
    exceptional = has_exceptional_state(repo, parsed)
    return RepoState(
        parsed=parsed,
        additions=additions,
        deletions=deletions,
        ahead=ahead,
        behind=behind,
        remote_ref=remote_ref,
        remote_url=remote_url,
        branch_name=branch_name,
        exceptional=exceptional,
        submodule_count=count_submodules(repo),
        ident=read_git_ident(repo),
        latest_mtime=latest_mtime,
        dirty=bool(
            parsed.modified_count
            or additions
            or deletions
            or parsed.untracked_count
            or parsed.deleted_count
            or exceptional
        ),
        worktree_status_checked=not metadata_only,
    )


def _can_pull_safely(state: RepoState, *, fetch_failed: bool) -> bool:
    return (
        not fetch_failed
        and state.worktree_status_checked
        and state.behind > 0
        and state.ahead == 0
        and not state.dirty
        and state.remote_ref is not None
    )


def _can_push_safely(state: RepoState, *, fetch_failed: bool) -> bool:
    return (
        not fetch_failed
        and state.worktree_status_checked
        and state.ahead > 0
        and state.behind == 0
        and not state.dirty
        and state.remote_ref is not None
    )


def _upstream_push_target(repo: Repo) -> tuple[str, str] | None:
    if repo.head.is_detached:
        return None
    try:
        tracking = repo.active_branch.tracking_branch()
    except (TypeError, GitCommandError):
        return None
    if tracking is None:
        return None
    remote_name = tracking.remote_name
    remote_branch = tracking.remote_head
    if not remote_name or not remote_branch:
        return None
    if remote_name not in {remote.name for remote in repo.remotes}:
        return None
    return remote_name, remote_branch


def _render_repo_state_segments(
    state: RepoState,
    *,
    pulled: bool,
    pushed: bool = False,
) -> list[tuple[str, str | None, str]]:
    segments: list[tuple[str, str | None, str]] = []
    if not state.worktree_status_checked:
        segments.append(("?", "bright_black", "core"))
    if state.parsed.modified_count:
        segments.append((f"{state.parsed.modified_count}m", "yellow", "core"))
    if state.additions or state.deletions:
        segments.append((f"(+{state.additions}/-{state.deletions})", "cyan", "plus_minus"))
    if state.parsed.untracked_count:
        segments.append((f"{state.parsed.untracked_count}u", "magenta", "core"))
    if state.parsed.deleted_count:
        segments.append((f"{state.parsed.deleted_count}d", "red", "core"))
    if state.submodule_count:
        segments.append((f"{state.submodule_count}s", "blue", "extras"))
    if state.ahead:
        segments.append((f"{state.ahead}\u2191", "green", "core"))
    if state.behind:
        segments.append((f"{state.behind}\u2193", "bright_black", "core"))
    if pulled:
        segments.append(("P", "cyan", "extras"))
    if pushed:
        segments.append(("U", "green", "extras"))
    if state.exceptional:
        segments.append(("!", "bold red", "core"))
    return segments


def parse_status_porcelain(output: str) -> ParsedStatus:
    modified_paths: set[str] = set()
    deleted_paths: set[str] = set()
    untracked = 0
    has_conflicts = False
    has_tracked_changes = False
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        if code == "??":
            untracked += 1
            continue
        has_tracked_changes = True
        index_status, worktree_status = code
        if index_status == "U" or worktree_status == "U" or code in {"AA", "DD"}:
            has_conflicts = True
        if index_status == "D" or worktree_status == "D":
            deleted_paths.add(raw_line[3:])
        if any(
            status in {"M", "A", "R", "C", "T"}
            for status in (index_status, worktree_status)
        ):
            modified_paths.add(raw_line[3:])
    return ParsedStatus(
        modified_count=len(modified_paths),
        untracked_count=untracked,
        deleted_count=len(deleted_paths),
        has_conflicts=has_conflicts,
        has_tracked_changes=has_tracked_changes,
    )


def collect_status_snapshot(repo: Repo) -> StatusSnapshot:
    output = repo.git.status(
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=normal",
    )
    return parse_status_porcelain_v2(output)


def parse_status_porcelain_v2(output: str) -> StatusSnapshot:
    modified = 0
    untracked = 0
    deleted = 0
    has_conflicts = False
    has_tracked_changes = False
    branch_oid: str | None = None
    branch_head: str | None = None
    upstream: str | None = None
    ahead = behind = 0

    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            header, _, value = record[2:].partition(" ")
            if header == "branch.oid":
                branch_oid = value
            elif header == "branch.head":
                branch_head = value
            elif header == "branch.upstream":
                upstream = value
            elif header == "branch.ab":
                match = re.fullmatch(r"\+(\d+) -(\d+)", value)
                if match:
                    ahead = int(match.group(1))
                    behind = int(match.group(2))
            continue
        if record.startswith("? "):
            untracked += 1
            continue

        record_kind, _, remainder = record.partition(" ")
        if record_kind not in {"1", "2", "u"}:
            continue
        has_tracked_changes = True
        code, _, _ = remainder.partition(" ")
        if record_kind == "u" or code in {"AA", "DD"} or "U" in code:
            has_conflicts = True
        if "D" in code:
            deleted += 1
        if any(status in {"M", "A", "R", "C", "T"} for status in code):
            modified += 1
        if record_kind == "2":
            # With -z, a rename/copy record's original path is the next item.
            index += 1

    return StatusSnapshot(
        parsed=ParsedStatus(
            modified_count=modified,
            untracked_count=untracked,
            deleted_count=deleted,
            has_conflicts=has_conflicts,
            has_tracked_changes=has_tracked_changes,
        ),
        branch_oid=branch_oid,
        branch_head=branch_head,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
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


def snapshot_branch_name(snapshot: StatusSnapshot) -> str:
    if snapshot.branch_head == "(detached)":
        if snapshot.branch_oid and not snapshot.branch_oid.startswith("("):
            return f"DETACHED@{snapshot.branch_oid[:7]}"
        return "DETACHED"
    return snapshot.branch_head or "UNKNOWN"


def tracking_display(repo: Repo, upstream: str | None) -> tuple[str | None, str | None]:
    if upstream is None or repo.head.is_detached:
        return upstream, None
    try:
        tracking = repo.active_branch.tracking_branch()
    except (TypeError, GitCommandError):
        return upstream, None
    if tracking is None:
        return upstream, None
    remote_name = tracking.remote_name
    remote_head = tracking.remote_head
    if not remote_name or not remote_head:
        return upstream, None
    remote_ref = f"{remote_name}/{remote_head}"
    remote_url = format_remote_urls(repo, remote_name)
    return remote_ref, remote_url


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
        fetch_url_value = config.get_value(section, "url")
        fetch_url = str(fetch_url_value)
    except Exception:
        return None
    push_url = None
    try:
        push_url_value = config.get_value(section, "pushurl")
        push_url = str(push_url_value)
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
        "gitea.com": "gitea:",
        "git.mozilla.org": "moz:",
        "git.sr.ht": "sr.ht:",
        "pagure.io": "pg:",
        "git.kernel.org": "kernel:",
        "git.apache.org": "apache:",
        "git.savannah.gnu.org": "gnu:",
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
        pass
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        return str(path)
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(rel) == "." else f"~/{rel}"


def render_status_segments(segments: Sequence[tuple[str, str | None, str]]) -> str:
    status = " ".join(seg[0] for seg in segments)
    return status or "clean"


def latest_worktree_mtime(repo: Repo) -> float | None:
    worktree_dir = repo.working_tree_dir
    if worktree_dir is None:
        return None
    worktree = Path(worktree_dir).resolve()
    candidates: set[Path] = set()

    def _add_path(rel_path: str) -> None:
        if not rel_path:
            return
        if rel_path.startswith(".git/") or rel_path == ".git":
            return
        absolute = worktree / rel_path
        if not absolute.exists():
            return
        candidates.add(absolute)

    try:
        tracked = repo.git.ls_files("-z").split("\0")
    except GitCommandError:
        tracked = []
    for rel_path in tracked:
        _add_path(rel_path)

    try:
        untracked = repo.git.ls_files(
            "--others",
            "--exclude-standard",
            "-z",
        ).split("\0")
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


__all__ = [
    "ParsedStatus",
    "RepoReport",
    "StatusSnapshot",
    "collect_reports",
    "collect_reports_parallel",
    "collect_status_snapshot",
    "has_exceptional_state",
    "latest_worktree_mtime",
    "parse_status_porcelain",
    "parse_status_porcelain_v2",
    "render_status_segments",
    "simplify_url",
]
