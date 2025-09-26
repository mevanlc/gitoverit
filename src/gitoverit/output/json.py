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
            "status": render_status_segments(report.status_segments),
            "branch": report.branch,
            "remote": report.remote,
            "remote_url": report.remote_url,
            "ident": report.ident,
            "dirty": report.dirty,
            "latest_mtime": report.latest_mtime,
        }
        for report in reports
    ]
    return json.dumps(payload, indent=2, sort_keys=True)


__all__ = ["render_json"]
