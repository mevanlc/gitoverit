import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from git import Actor, Repo

from gitoverit.reporting import (
    ParsedStatus,
    has_exceptional_state,
    latest_worktree_mtime,
    parse_status_porcelain,
    simplify_url,
)


class ParseStatusTests(unittest.TestCase):
    def test_counts_modified_untracked_deleted(self) -> None:
        status = """ M file1.py\nM  file2.py\n?? newfile.txt\n D removed.txt\n"""
        parsed = parse_status_porcelain(status)
        self.assertEqual(parsed.modified_count, 2)
        self.assertEqual(parsed.untracked_count, 1)
        self.assertEqual(parsed.deleted_count, 1)
        self.assertFalse(parsed.has_conflicts)

    def test_detects_conflicts(self) -> None:
        status = "UU conflicted.txt\n"""
        parsed = parse_status_porcelain(status)
        self.assertTrue(parsed.has_conflicts)


class SimplifyUrlTests(unittest.TestCase):
    def test_github_https(self) -> None:
        self.assertEqual(simplify_url("https://github.com/owner/repo.git"), "owner/repo")

    def test_gitlab_ssh(self) -> None:
        self.assertEqual(simplify_url("git@gitlab.com:group/project.git"), "ssh+gl:group/project")

    def test_custom_domain(self) -> None:
        self.assertEqual(
            simplify_url("ssh://git@example.com/team/repo.git"),
            "ssh+example.com/team/repo",
        )


class LatestWorktreeMtimeTests(unittest.TestCase):
    def test_tracks_untracked_file_mtime(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            worktree = Path(tmpdir)

            tracked = worktree / "tracked.txt"
            tracked.write_text("tracked")
            repo.index.add([str(tracked.relative_to(worktree))])
            author = Actor("Tester", "tester@example.com")
            repo.index.commit("initial", author=author, committer=author)

            past_time = time.time() - 10_000
            os.utime(tracked, (past_time, past_time))

            untracked = worktree / "untracked.txt"
            untracked.write_text("untracked")
            future_time = time.time() + 10_000
            os.utime(untracked, (future_time, future_time))

            latest = latest_worktree_mtime(repo)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertGreaterEqual(latest, future_time - 0.01)


class ExceptionalStateTests(unittest.TestCase):
    def test_stale_rebase_head_ignored(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            worktree = Path(tmpdir)
            tracked = worktree / "tracked.txt"
            tracked.write_text("tracked")
            repo.index.add([str(tracked.relative_to(worktree))])
            author = Actor("Tester", "tester@example.com")
            repo.index.commit("initial", author=author, committer=author)

            rebase_head = Path(repo.git_dir) / "REBASE_HEAD"
            rebase_head.write_text(repo.head.commit.hexsha + "\n")

            parsed = ParsedStatus(0, 0, 0, False)
            self.assertFalse(has_exceptional_state(repo, parsed))


if __name__ == "__main__":
    unittest.main()
