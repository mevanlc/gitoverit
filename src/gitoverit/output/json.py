from __future__ import annotations

import json
from typing import Sequence

from ..reporting import RepoReport, render_status_segments


def render_json(reports: Sequence[RepoReport]) -> str:
    payload = [
        {
            "path": str(report.path),
            "display_path": report.display_path,
            "fetch_failed": report.fetch_failed,
            "pull_failed": report.pull_failed,
            "pulled": report.pulled,
            "status": render_status_segments(report.status_segments),
            "worktree_status_checked": report.worktree_status_checked,
            "branch": report.branch,
            "remote": report.remote,
            "remote_url": report.remote_url,
            "ident": report.ident,
            "dirty": report.dirty,
            "ahead": report.ahead,
            "behind": report.behind,
            "modified": report.modified,
            "untracked": report.untracked,
            "deleted": report.deleted,
            "mtime": report.latest_mtime,
            "latest_mtime": report.latest_mtime,
        }
        for report in reports
    ]
    return json.dumps(payload, indent=2, sort_keys=True)


__all__ = ["render_json"]
