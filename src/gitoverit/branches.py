from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from git import Repo

from .config import apply_id_rewrites
from .reporting import _collect_repo_state, _render_repo_state_segments


class BranchCollectionError(RuntimeError):
    pass


@dataclass
class BranchReport:
    repo_path: Path
    branch: str
    head: str
    upstream: str
    has_upstream: bool
    ahead: int
    behind: int
    gone: bool
    current: bool
    worktree_path: Path | None
    worktree_display: str
    author: str
    author_fg: str | None
    date: int
    subject: str
    # (value, style, narrow_class); narrow_class ∈ {"core", "extras", "plus_minus"}
    status_segments: Sequence[tuple[str, str | None, str]] = field(default_factory=list)
    modified: int = 0
    untracked: int = 0
    deleted: int = 0
    dirty: bool = False
    upstream_remote: str | None = None
    # Fetch URL when push URL == fetch URL; None otherwise (so callers can
    # decide not to surface a link when a divergent push URL would mislead).
    upstream_link_url: str | None = None


def collect_branch_reports(repo_dir: Path, *, fetch: bool = False) -> list[BranchReport]:
    repo_root = _resolve_repo_root(repo_dir)

    if fetch:
        _run_git(repo_root, "fetch", "--all", "--prune")

    current_branch = _current_branch_name(repo_root)
    worktrees_by_branch = _worktrees_by_branch_ref(repo_root)
    upstreams_by_branch = _upstreams_by_branch(repo_root)
    remote_urls = _remote_urls(repo_root)

    format_string = (
        "%(refname)%00%(refname:short)%00%(objectname:short=12)%00"
        "%(authorname)%00%(authoremail)%00%(committerdate:unix)%00%(subject)"
    )
    output = _run_git(repo_root, "for-each-ref", f"--format={format_string}", "refs/heads")

    reports: list[BranchReport] = []
    for line in output.splitlines():
        if not line:
            continue

        refname, branch, head, author_name, author_email, date_text, subject = line.split("\0")
        upstream = upstreams_by_branch.get(branch)
        has_upstream = upstream is not None

        upstream_remote: str | None = None
        upstream_link_url: str | None = None
        if upstream is None:
            upstream_display = "-"
            gone = False
            ahead = 0
            behind = 0
        else:
            upstream_display, upstream_full, upstream_remote = upstream
            gone = not _git_ref_exists(repo_root, upstream_full)
            if gone:
                ahead = 0
                behind = 0
            else:
                ahead, behind = _ahead_behind(repo_root, refname, upstream_full)
            if upstream_remote and upstream_remote in remote_urls:
                fetch_url, push_url = remote_urls[upstream_remote]
                if fetch_url and fetch_url == push_url:
                    upstream_link_url = _browse_url(fetch_url)

        worktree_path = worktrees_by_branch.get(refname)
        current = current_branch == branch

        status_segments, modified, untracked, deleted, dirty = _branch_status(
            worktree_path=worktree_path,
            ahead=ahead,
            behind=behind,
        )

        author, author_fg = _author_identity(author_name, author_email)

        reports.append(
            BranchReport(
                repo_path=repo_root,
                branch=branch,
                head=head,
                upstream=upstream_display,
                has_upstream=has_upstream,
                ahead=ahead,
                behind=behind,
                gone=gone,
                current=current,
                worktree_path=worktree_path,
                worktree_display=_worktree_display(repo_root, worktree_path),
                author=author,
                author_fg=author_fg,
                date=int(date_text) if date_text else 0,
                subject=subject,
                status_segments=status_segments,
                modified=modified,
                untracked=untracked,
                deleted=deleted,
                dirty=dirty,
                upstream_remote=upstream_remote,
                upstream_link_url=upstream_link_url,
            )
        )

    return reports


def _branch_status(
    *,
    worktree_path: Path | None,
    ahead: int,
    behind: int,
) -> tuple[list[tuple[str, str | None, str]], int, int, int, bool]:
    """Return (status_segments, modified, untracked, deleted, dirty) for a branch.

    Branches with a worktree get the full repo status (dirty state,
    ahead/behind, exceptional markers). Branches without a worktree get only
    ahead/behind, and no dirty counts.
    """
    if worktree_path is not None:
        try:
            state = _collect_repo_state(Repo(worktree_path))
        except Exception:
            state = None
        if state is not None:
            segments = _render_repo_state_segments(state, pulled=False)
            return (
                segments,
                state.parsed.modified_count,
                state.parsed.untracked_count,
                state.parsed.deleted_count,
                state.dirty,
            )

    segments: list[tuple[str, str | None, str]] = []
    if ahead:
        segments.append((f"{ahead}↑", "green", "core"))
    if behind:
        segments.append((f"{behind}↓", "bright_black", "core"))
    return segments, 0, 0, 0, False


def _run_git(repo_dir: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise BranchCollectionError(f"{repo_dir}: {details.splitlines()[-1]}")
    return result.stdout


def _run_git_ok(repo_dir: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _resolve_repo_root(repo_dir: Path) -> Path:
    output = _run_git(repo_dir, "rev-parse", "--show-toplevel")
    return Path(output.strip()).resolve()


def _current_branch_name(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _worktrees_by_branch_ref(repo_root: Path) -> dict[str, Path]:
    output = _run_git(repo_root, "worktree", "list", "--porcelain")
    mapping: dict[str, Path] = {}

    for block in output.strip().split("\n\n"):
        if not block.strip():
            continue

        worktree_path: Path | None = None
        branch_ref: str | None = None

        for line in block.splitlines():
            if line.startswith("worktree "):
                worktree_path = Path(line.split(" ", 1)[1]).resolve()
            elif line.startswith("branch "):
                branch_ref = line.split(" ", 1)[1]

        if worktree_path is not None and branch_ref is not None:
            mapping[branch_ref] = worktree_path

    return mapping


def _upstreams_by_branch(repo_root: Path) -> dict[str, tuple[str, str, str | None]]:
    result = subprocess.run(
        ["git", "config", "--get-regexp", r"^branch\..*\.(remote|merge)$"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    raw: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, value = line.split(None, 1)
        remainder = key.removeprefix("branch.")
        branch, config_field = remainder.rsplit(".", 1)
        raw.setdefault(branch, {})[config_field] = value.strip()

    upstreams: dict[str, tuple[str, str, str | None]] = {}
    for branch, config in raw.items():
        remote = config.get("remote")
        merge = config.get("merge")
        if remote is None or merge is None:
            continue
        upstreams[branch] = _upstream_from_config(remote, merge)

    return upstreams


def _upstream_from_config(remote: str, merge: str) -> tuple[str, str, str | None]:
    merge_short = _short_refname(merge)
    if remote == ".":
        return merge_short, merge, None
    return (
        f"{remote}/{merge_short}",
        f"refs/remotes/{remote}/{merge_short}",
        remote,
    )


def _remote_urls(repo_root: Path) -> dict[str, tuple[str, str]]:
    """Return {remote_name: (fetch_url, push_url)} parsed from `git remote -v`.

    When no explicit pushurl is configured, git reports fetch==push in
    `remote -v` output, so equality is the natural signal for "push matches
    fetch" without a separate config probe.
    """
    output = _run_git(repo_root, "remote", "-v", check=False)
    urls: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, url, kind = parts[0], parts[1], parts[2]
        if kind == "(fetch)":
            urls.setdefault(name, {})["fetch"] = url
        elif kind == "(push)":
            urls.setdefault(name, {})["push"] = url

    result: dict[str, tuple[str, str]] = {}
    for name, entry in urls.items():
        fetch = entry.get("fetch", "")
        push = entry.get("push", fetch)
        result[name] = (fetch, push)
    return result


_SCP_URL = re.compile(r"^(?P<user>[^@/:]+)@(?P<host>[^:/]+):(?P<path>.+)$")


def _browse_url(url: str) -> str | None:
    """Convert a git URL to an https URL that terminals/browsers can open.

    Returns None when the URL can't be safely rewritten (rare schemes, empty
    input). For hyperlink purposes we prefer a rewrite that will actually open
    in a browser over passing through the raw URL.
    """
    if not url:
        return None
    if url.startswith(("https://", "http://")):
        return url.removesuffix(".git")

    match = _SCP_URL.match(url)
    if match:
        host = match.group("host")
        path = match.group("path").removesuffix(".git")
        return f"https://{host}/{path}"

    if url.startswith("ssh://"):
        rest = url[len("ssh://"):]
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        if "/" not in rest:
            return None
        host, path = rest.split("/", 1)
        host = host.split(":", 1)[0]
        return f"https://{host}/{path.removesuffix('.git')}"

    if url.startswith("git://"):
        rest = url[len("git://"):]
        if "/" not in rest:
            return None
        host, path = rest.split("/", 1)
        return f"https://{host}/{path.removesuffix('.git')}"

    return None


def _short_refname(refname: str) -> str:
    for prefix in ("refs/heads/", "refs/remotes/", "refs/tags/"):
        if refname.startswith(prefix):
            return refname[len(prefix):]
    if refname.startswith("refs/"):
        return refname[len("refs/"):]
    return refname


def _git_ref_exists(repo_root: Path, refname: str) -> bool:
    return _run_git_ok(repo_root, "rev-parse", "--verify", "--quiet", f"{refname}^{{commit}}")


def _ahead_behind(repo_root: Path, branch_ref: str, upstream_ref: str) -> tuple[int, int]:
    output = _run_git(repo_root, "rev-list", "--left-right", "--count", f"{branch_ref}...{upstream_ref}")
    ahead_text, behind_text = output.strip().split()
    return int(ahead_text), int(behind_text)


def _author_identity(author_name: str, author_email: str) -> tuple[str, str | None]:
    identity = author_name if not author_email else f"{author_name} {author_email}"
    result = apply_id_rewrites(identity)
    return result.value, result.fg


def _worktree_display(repo_root: Path, worktree_path: Path | None) -> str:
    if worktree_path is None:
        return "-"
    if worktree_path == repo_root:
        return "here"
    return os.path.relpath(worktree_path, repo_root.parent)


__all__ = ["BranchCollectionError", "BranchReport", "collect_branch_reports"]
