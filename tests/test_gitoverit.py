import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from git import Actor, Repo

from gitoverit.output.table import DEFAULT_COLUMNS, parse_columns
from gitoverit.reporting import (
    ParsedStatus,
    discover_repositories,
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


class ParseColumnsTests(unittest.TestCase):
    def test_no_spec_returns_default(self) -> None:
        # Empty-ish spec should return default columns unchanged
        self.assertEqual(parse_columns(""), DEFAULT_COLUMNS)

    def test_remove_single(self) -> None:
        result = parse_columns("-ident")
        self.assertEqual(result, ["dir", "status", "branch", "remote", "url"])

    def test_remove_multiple(self) -> None:
        result = parse_columns("-ident,-remote")
        self.assertEqual(result, ["dir", "status", "branch", "url"])

    def test_clear_then_add(self) -> None:
        result = parse_columns("-,url,branch,status,dir")
        self.assertEqual(result, ["url", "branch", "status", "dir"])

    def test_last_mention_wins_readd(self) -> None:
        # Remove then re-add → included, appended at end
        result = parse_columns("-dir,dir")
        self.assertEqual(result, ["status", "branch", "remote", "url", "ident", "dir"])

    def test_last_mention_wins_remove(self) -> None:
        # Add then remove → excluded
        result = parse_columns("-,dir,-dir")
        self.assertEqual(result, [])

    def test_add_moves_to_end(self) -> None:
        # Mentioning an existing column moves it to the end
        result = parse_columns("dir")
        self.assertEqual(result, ["status", "branch", "remote", "url", "ident", "dir"])

    def test_unknown_column_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_columns("bogus")

    def test_unknown_removal_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_columns("-bogus")


class DiscoverRepositoriesTests(unittest.TestCase):
    def test_gitignored_nested_repo_is_skipped(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            parent_repo = Repo.init(parent)

            # Create a .gitignore that ignores the nested dir
            gitignore = parent / ".gitignore"
            gitignore.write_text("nested/\n")
            parent_repo.index.add([".gitignore"])
            author = Actor("Tester", "tester@example.com")
            parent_repo.index.commit("init", author=author, committer=author)

            # Create a nested repo inside the gitignored directory
            nested = parent / "nested"
            nested.mkdir()
            Repo.init(nested)

            discovered = list(discover_repositories([parent]))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertNotIn(nested.resolve(), resolved_paths)

    def test_non_gitignored_nested_repo_is_found(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            Repo.init(parent)

            # Nested repo that is NOT gitignored
            nested = parent / "nested"
            nested.mkdir()
            Repo.init(nested)

            discovered = list(discover_repositories([parent]))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertIn(nested.resolve(), resolved_paths)

    def test_deeply_nested_under_gitignored_also_skipped(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            parent_repo = Repo.init(parent)

            gitignore = parent / ".gitignore"
            gitignore.write_text("vendor/\n")
            parent_repo.index.add([".gitignore"])
            author = Actor("Tester", "tester@example.com")
            parent_repo.index.commit("init", author=author, committer=author)

            # Create vendor/lib which is a repo, under a gitignored path
            vendor_lib = parent / "vendor" / "lib"
            vendor_lib.mkdir(parents=True)
            Repo.init(vendor_lib)

            discovered = list(discover_repositories([parent]))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertNotIn(vendor_lib.resolve(), resolved_paths)


if __name__ == "__main__":
    unittest.main()
