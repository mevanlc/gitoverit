import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from typer.testing import CliRunner

from gitoverit.branches import collect_branch_reports
from gitoverit.branches_cli import APP

RUNNER = CliRunner()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def init_repo(path: Path) -> None:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.name", "Tester")
    git(path, "config", "user.email", "tester@example.com")
    (path / "tracked.txt").write_text("tracked")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "initial")


class BranchCollectorTests(unittest.TestCase):
    def test_collect_marks_current_branch_and_no_upstream(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            git(repo, "branch", "topic")
            current_branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()

            reports = collect_branch_reports(repo)
            by_branch = {report.branch: report for report in reports}

            self.assertIn(current_branch, by_branch)
            self.assertIn("topic", by_branch)
            self.assertTrue(by_branch[current_branch].current)
            self.assertEqual(by_branch[current_branch].worktree_display, "here")
            self.assertFalse(by_branch[current_branch].has_upstream)
            self.assertFalse(by_branch[current_branch].gone)
            self.assertEqual(by_branch[current_branch].upstream, "-")
            self.assertEqual(by_branch["topic"].worktree_display, "-")

    def test_detached_head_marks_no_branch_current(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            git(repo, "branch", "topic")
            git(repo, "checkout", "--detach", "HEAD")

            reports = collect_branch_reports(repo)

            self.assertTrue(reports)
            self.assertTrue(all(not report.current for report in reports))

    def test_linked_worktree_is_reported_and_here_is_relative_to_target_worktree(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            worktree = root / "wt-topic"
            init_repo(repo)
            git(repo, "branch", "topic")
            git(repo, "worktree", "add", str(worktree), "topic")

            main_reports = collect_branch_reports(repo)
            main_topic = {report.branch: report for report in main_reports}["topic"]
            self.assertEqual(main_topic.worktree_path, worktree.resolve())
            self.assertNotEqual(main_topic.worktree_display, "-")
            self.assertFalse(main_topic.current)

            linked_reports = collect_branch_reports(worktree)
            linked_topic = {report.branch: report for report in linked_reports}["topic"]
            self.assertTrue(linked_topic.current)
            self.assertEqual(linked_topic.worktree_display, "here")

    def test_missing_remote_tracking_ref_marks_upstream_gone(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            git(repo, "branch", "topic")
            git(repo, "config", "branch.topic.remote", "origin")
            git(repo, "config", "branch.topic.merge", "refs/heads/topic")

            reports = collect_branch_reports(repo)
            topic = {report.branch: report for report in reports}["topic"]

            self.assertTrue(topic.has_upstream)
            self.assertEqual(topic.upstream, "origin/topic")
            self.assertTrue(topic.gone)
            self.assertEqual(topic.ahead, 0)
            self.assertEqual(topic.behind, 0)


class BranchCliTests(unittest.TestCase):
    def test_help_renders_real_command_help(self) -> None:
        result = RUNNER.invoke(APP, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--glob", result.stdout)
        self.assertIn("--json", result.stdout)

    def test_json_output_includes_branch_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            git(repo, "branch", "topic")

            result = RUNNER.invoke(APP, [str(repo), "--json"])
            self.assertEqual(result.exit_code, 0)

            payload = json.loads(result.stdout)
            branches = {item["branch"]: item for item in payload}
            current_branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()

            self.assertIn(current_branch, branches)
            self.assertIn("topic", branches)
            self.assertIn("worktree", branches[current_branch])
            self.assertIn("current", branches[current_branch])

    def test_glob_and_print_filter_branch_names(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            git(repo, "branch", "topic")
            git(repo, "branch", "bugfix/demo")

            result = RUNNER.invoke(
                APP,
                [str(repo), "--glob", "top*", "--print", "branch"],
            )
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "topic")


if __name__ == "__main__":
    unittest.main()
