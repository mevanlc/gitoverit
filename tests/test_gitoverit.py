import json
import io
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from traceback import TracebackException
from unittest.mock import patch

from git import Actor, Git, Repo
from simpleeval import FeatureNotAvailable
from typer.testing import CliRunner

from gitoverit.output import render_json
from gitoverit.output.table import (
    DEFAULT_COLUMNS,
    METADATA_ONLY_DEFAULT_COLUMNS,
    parse_columns,
)
from gitoverit.repos_cli import (
    APP,
    SortMode,
    _filter_reports,
    _print_reports,
    _sort_reports,
)
from gitoverit.reporting import (
    ParsedStatus,
    RepoReport,
    analyze_repository,
    collect_reports,
    discover_repositories,
    has_exceptional_state,
    latest_worktree_mtime,
    parse_status_porcelain,
    parse_status_porcelain_v2,
    simplify_url,
)

AUTHOR = Actor("Tester", "tester@example.com")
RUNNER = CliRunner()


class ReposCliTests(unittest.TestCase):
    def test_help_renders_repo_command_help(self) -> None:
        result = RUNNER.invoke(APP, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Scan git repositories beneath the given directories", result.stdout)
        self.assertIn("--metadata-only", result.stdout)
        self.assertIn("--nested", result.stdout)
        self.assertIn("--push-safe", result.stdout)
        self.assertIn("--id-columns", result.stdout)
        self.assertIn("--verbose", result.stdout)
        self.assertIn("FIELD", result.stdout)
        self.assertIn("Valid fields:", result.stdout)
        for sort_mode in SortMode:
            self.assertIn(sort_mode.value, result.stdout)

    def test_help_where_renders_expression_help(self) -> None:
        result = RUNNER.invoke(APP, ["--help-where"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Filter expressions for --where / -w", result.stdout)

    def test_pull_safe_short_alias(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]) as collect,
            patch("gitoverit.repos_cli.render_table"),
        ):
            result = RUNNER.invoke(APP, ["-P", "--no-progress"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(collect.call_args.kwargs["fetch"])
        self.assertTrue(collect.call_args.kwargs["pull_safe"])

    def test_push_safe_short_alias_enables_fetch_and_push(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]) as collect,
            patch("gitoverit.repos_cli.render_table"),
        ):
            result = RUNNER.invoke(APP, ["-U", "--no-progress"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(collect.call_args.kwargs["fetch"])
        self.assertTrue(collect.call_args.kwargs["push_safe"])

    def test_pull_safe_and_push_safe_can_be_combined(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]) as collect,
            patch("gitoverit.repos_cli.render_table"),
        ):
            result = RUNNER.invoke(
                APP,
                ["--pull-safe", "--push-safe", "--no-progress"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(collect.call_args.kwargs["fetch"])
        self.assertTrue(collect.call_args.kwargs["pull_safe"])
        self.assertTrue(collect.call_args.kwargs["push_safe"])

    def test_cli_skips_mtime_when_output_and_sort_do_not_need_it(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]) as collect,
            patch("gitoverit.repos_cli.render_table"),
        ):
            result = RUNNER.invoke(
                APP,
                ["--sort", "dir", "--columns=-mtime", "--no-progress"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(collect.call_args.kwargs["collect_mtime"])

    def test_cli_keeps_mtime_for_json_output(self) -> None:
        with patch(
            "gitoverit.repos_cli.collect_reports_parallel",
            return_value=[],
        ) as collect:
            result = RUNNER.invoke(
                APP,
                ["--sort", "dir", "--format", "json", "--no-progress"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(collect.call_args.kwargs["collect_mtime"])

    def test_cli_passes_id_columns_to_render_table(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]),
            patch("gitoverit.repos_cli._sort_reports"),
            patch("gitoverit.repos_cli.render_table") as render_table,
        ):
            result = RUNNER.invoke(APP, ["--id-columns", "--no-progress"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(render_table.call_args.kwargs["id_columns"])

    def test_errors_are_terse_unless_verbose(self) -> None:
        def collect_with_error(*args, **kwargs):
            hook = kwargs["hook"]
            try:
                raise RuntimeError("status failed\n    detail")
            except RuntimeError as exc:
                hook.error(Path("broken-repo"), TracebackException.from_exception(exc))
            return []

        for extra_args, traceback_expected in (([], False), (["--verbose"], True)):
            with (
                self.subTest(extra_args=extra_args),
                patch(
                    "gitoverit.repos_cli.collect_reports_parallel",
                    side_effect=collect_with_error,
                ),
                patch("gitoverit.repos_cli.render_table"),
            ):
                result = RUNNER.invoke(
                    APP,
                    ["--errors", "--no-progress", *extra_args],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("broken-repo", result.output)
            self.assertIn("RuntimeError: status failed", result.output)
            self.assertIn("detail", result.output)
            self.assertEqual(
                "Traceback (most recent call last):" in result.output,
                traceback_expected,
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

    def test_parses_porcelain_v2_status_and_branch_headers(self) -> None:
        status = (
            "# branch.oid 0123456789abcdef\0"
            "# branch.head feature\0"
            "# branch.upstream origin/main\0"
            "# branch.ab +2 -3\0"
            "1 .M N... 100644 100644 100644 aaaaaaa aaaaaaa tracked.txt\0"
            "1 .D N... 100644 100644 000000 aaaaaaa aaaaaaa deleted.txt\0"
            "2 R. N... 100644 100644 100644 aaaaaaa aaaaaaa R100 renamed.txt\0"
            "? old-name-that-must-be-skipped\0"
            "? untracked/\0"
            "u UU N... 100644 100644 100644 100644 a a a conflict.txt\0"
        )

        snapshot = parse_status_porcelain_v2(status)

        self.assertEqual(snapshot.branch_oid, "0123456789abcdef")
        self.assertEqual(snapshot.branch_head, "feature")
        self.assertEqual(snapshot.upstream, "origin/main")
        self.assertEqual((snapshot.ahead, snapshot.behind), (2, 3))
        self.assertEqual(snapshot.parsed.modified_count, 2)
        self.assertEqual(snapshot.parsed.deleted_count, 1)
        self.assertEqual(snapshot.parsed.untracked_count, 1)
        self.assertTrue(snapshot.parsed.has_conflicts)
        self.assertTrue(snapshot.parsed.has_tracked_changes)


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
            repo.index.commit("initial", author=AUTHOR, committer=AUTHOR)

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

    def test_ignored_files_and_parent_directories_do_not_affect_mtime(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            worktree = Path(tmpdir)
            tracked = worktree / "tracked.txt"
            gitignore = worktree / ".gitignore"
            tracked.write_text("tracked")
            gitignore.write_text("ignored/\n")
            repo.index.add(["tracked.txt", ".gitignore"])
            repo.index.commit("initial", author=AUTHOR, committer=AUTHOR)

            past_time = time.time() - 10_000
            os.utime(tracked, (past_time, past_time))
            os.utime(gitignore, (past_time, past_time))
            ignored = worktree / "ignored"
            ignored.mkdir()
            ignored_file = ignored / "generated.txt"
            ignored_file.write_text("generated")
            future_time = time.time() + 10_000
            os.utime(ignored_file, (future_time, future_time))

            latest = latest_worktree_mtime(repo)

            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertLess(latest, future_time - 1)


class AnalysisOptimizationTests(unittest.TestCase):
    def _clean_repo(self, tmpdir: str) -> Repo:
        repo = Repo.init(tmpdir)
        tracked = Path(tmpdir) / "tracked.txt"
        tracked.write_text("tracked\n")
        repo.index.add(["tracked.txt"])
        repo.index.commit("initial", author=AUTHOR, committer=AUTHOR)
        return repo

    def test_clean_repo_skips_diff_numstat(self) -> None:
        with TemporaryDirectory() as tmpdir:
            self._clean_repo(tmpdir)
            with patch(
                "gitoverit.reporting.diff_numstat_totals",
                side_effect=AssertionError("clean repository must not run git diff"),
            ):
                report = analyze_repository(
                    Path(tmpdir),
                    fetch=False,
                    collect_mtime=False,
                )

            self.assertFalse(report.dirty)

    def test_untracked_only_repo_skips_diff_numstat(self) -> None:
        with TemporaryDirectory() as tmpdir:
            self._clean_repo(tmpdir)
            Path(tmpdir, "untracked.txt").write_text("untracked\n")
            with patch(
                "gitoverit.reporting.diff_numstat_totals",
                side_effect=AssertionError("untracked-only repository must not run git diff"),
            ):
                report = analyze_repository(
                    Path(tmpdir),
                    fetch=False,
                    collect_mtime=False,
                )

            self.assertTrue(report.dirty)
            self.assertEqual(report.untracked, 1)

    def test_status_explicitly_includes_untracked_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = self._clean_repo(tmpdir)
            repo.git.config("status.showUntrackedFiles", "no")
            Path(tmpdir, "untracked.txt").write_text("untracked\n")

            report = analyze_repository(
                Path(tmpdir),
                fetch=False,
                collect_mtime=False,
            )

            self.assertTrue(report.dirty)
            self.assertEqual(report.untracked, 1)

    def test_collect_mtime_false_skips_worktree_mtime_scan(self) -> None:
        with TemporaryDirectory() as tmpdir:
            self._clean_repo(tmpdir)
            with patch(
                "gitoverit.reporting.latest_worktree_mtime",
                side_effect=AssertionError("mtime scan must not run"),
            ):
                report = analyze_repository(
                    Path(tmpdir),
                    fetch=False,
                    collect_mtime=False,
                )

            self.assertIsNone(report.latest_mtime)


class MetadataOnlyTests(unittest.TestCase):
    def test_analysis_skips_worktree_operations_and_marks_values_unknown(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            tracked = Path(tmpdir) / "tracked.txt"
            tracked.write_text("tracked\n")
            repo.index.add(["tracked.txt"])
            repo.index.commit("initial", author=AUTHOR, committer=AUTHOR)

            original_getattr = Git.__getattr__

            def reject_status(git: Git, name: str):
                if name == "status":
                    raise AssertionError("git status must not run in metadata-only mode")
                return original_getattr(git, name)

            with (
                patch.object(Git, "__getattr__", reject_status),
                patch(
                    "gitoverit.reporting.diff_numstat_totals",
                    side_effect=AssertionError("diff must not run"),
                ),
                patch(
                    "gitoverit.reporting.latest_worktree_mtime",
                    side_effect=AssertionError("mtime scan must not run"),
                ),
            ):
                report = analyze_repository(
                    Path(tmpdir),
                    fetch=False,
                    metadata_only=True,
                )

            self.assertFalse(report.worktree_status_checked)
            self.assertIsNone(report.dirty)
            self.assertIsNone(report.modified)
            self.assertIsNone(report.untracked)
            self.assertIsNone(report.deleted)
            self.assertIsNone(report.latest_mtime)
            self.assertEqual(report.branch, repo.active_branch.name)
            self.assertIn(("?", "bright_black", "core"), report.status_segments)

            payload = json.loads(render_json([report]))[0]
            self.assertFalse(payload["worktree_status_checked"])
            self.assertEqual(payload["status"], "?")
            self.assertIsNone(payload["dirty"])
            self.assertIsNone(payload["modified"])
            self.assertIsNone(payload["untracked"])
            self.assertIsNone(payload["deleted"])
            self.assertIsNone(payload["mtime"])

    def test_cli_defaults_to_dir_sort_and_metadata_columns(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]) as collect,
            patch("gitoverit.repos_cli._sort_reports") as sort_reports,
            patch("gitoverit.repos_cli.render_table") as render_table,
        ):
            result = RUNNER.invoke(APP, ["--metadata-only", "--no-progress"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(collect.call_args.kwargs["metadata_only"])
        sort_reports.assert_called_once_with([], sort=SortMode.DIR, reverse=False)
        self.assertEqual(
            render_table.call_args.kwargs["columns"],
            METADATA_ONLY_DEFAULT_COLUMNS,
        )

    def test_cli_keeps_mtime_as_normal_default_sort(self) -> None:
        with (
            patch("gitoverit.repos_cli.collect_reports_parallel", return_value=[]),
            patch("gitoverit.repos_cli._sort_reports") as sort_reports,
            patch("gitoverit.repos_cli.render_table"),
        ):
            result = RUNNER.invoke(APP, ["--no-progress"])

        self.assertEqual(result.exit_code, 0, result.output)
        sort_reports.assert_called_once_with([], sort=SortMode.MTIME, reverse=False)

    def test_cli_rejects_options_that_require_worktree_status(self) -> None:
        cases = (
            ["--dirty-only"],
            ["--pull-safe"],
            ["--push-safe"],
            ["--sort", "mtime"],
            ["--where", "dirty"],
            ["--where", "modified > 0"],
            ["--print", "mtime"],
        )
        for args in cases:
            with self.subTest(args=args):
                result = RUNNER.invoke(
                    APP,
                    ["--metadata-only", "--no-progress", *args],
                )
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("cannot", result.output)


class ExceptionalStateTests(unittest.TestCase):
    def test_stale_rebase_head_ignored(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            worktree = Path(tmpdir)
            tracked = worktree / "tracked.txt"
            tracked.write_text("tracked")
            repo.index.add([str(tracked.relative_to(worktree))])
            repo.index.commit("initial", author=AUTHOR, committer=AUTHOR)

            rebase_head = Path(repo.git_dir) / "REBASE_HEAD"
            rebase_head.write_text(repo.head.commit.hexsha + "\n")

            parsed = ParsedStatus(0, 0, 0, False)
            self.assertFalse(has_exceptional_state(repo, parsed))


class ParseColumnsTests(unittest.TestCase):
    def test_no_spec_returns_default(self) -> None:
        # Empty-ish spec should return default columns unchanged
        self.assertEqual(parse_columns(""), DEFAULT_COLUMNS)

    def test_remove_single(self) -> None:
        result = parse_columns("-mtime")
        self.assertEqual(result, ["dir", "status", "branch", "remote", "url"])

    def test_remove_multiple(self) -> None:
        result = parse_columns("-mtime,-remote")
        self.assertEqual(result, ["dir", "status", "branch", "url"])

    def test_clear_then_add(self) -> None:
        result = parse_columns("-,url,branch,status,dir")
        self.assertEqual(result, ["url", "branch", "status", "dir"])

    def test_last_mention_wins_readd(self) -> None:
        # Remove then re-add → included, appended at end
        result = parse_columns("-dir,dir")
        self.assertEqual(result, ["status", "branch", "remote", "url", "mtime", "dir"])

    def test_last_mention_wins_remove(self) -> None:
        # Add then remove → excluded
        result = parse_columns("-,dir,-dir")
        self.assertEqual(result, [])

    def test_add_moves_to_end(self) -> None:
        # Mentioning an existing column moves it to the end
        result = parse_columns("dir")
        self.assertEqual(result, ["status", "branch", "remote", "url", "mtime", "dir"])

    def test_non_default_columns_can_be_added(self) -> None:
        result = parse_columns("branch_remote,ident")
        self.assertEqual(
            result, ["dir", "status", "branch", "remote", "url", "mtime", "branch_remote", "ident"]
        )

    def test_unknown_column_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_columns("bogus")

    def test_unknown_removal_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_columns("-bogus")

    def test_custom_defaults_are_used_as_column_base(self) -> None:
        result = parse_columns("ident", defaults=METADATA_ONLY_DEFAULT_COLUMNS)
        self.assertEqual(
            result,
            ["dir", "status", "branch", "remote", "url", "ident"],
        )


class DiscoverRepositoriesTests(unittest.TestCase):
    def _repo(self, path: Path, *, commit: bool = False) -> Repo:
        path.mkdir(parents=True, exist_ok=True)
        repo = Repo.init(path)
        if commit:
            (path / "tracked.txt").write_text("initial\n")
            repo.index.add(["tracked.txt"])
            repo.index.commit("init", author=AUTHOR, committer=AUTHOR)
        return repo

    def _submodule(self, parent: Path, style: str) -> Path:
        repo = self._repo(parent)
        submodule = parent / "deps" / "sub module"
        relative = submodule.relative_to(parent).as_posix()
        if style == "absorbed":
            source = parent.parent / "source"
            self._repo(source, commit=True)
            repo.git.execute([
                "git", "-c", "protocol.file.allow=always", "submodule", "add",
                str(source), relative,
            ])
        else:
            child = self._repo(submodule, commit=True)
            if style == "gitlink":
                repo.git.update_index(
                    "--add", "--cacheinfo", f"160000,{child.head.commit.hexsha},{relative}"
                )
            else:
                repo.git.config(
                    "--file", str(parent / ".gitmodules"), "submodule.example.path", relative
                )
        return submodule

    def test_depth_counts_repositories_and_resets_for_siblings(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            top = root / "holder" / "top"
            other_top = root / "holder" / "other"
            first = top / "ordinary" / "directories" / "first"
            second = first / "another" / "second"
            sibling = top / "sibling"
            for path in (top, other_top, first, second, sibling):
                self._repo(path)

            self.assertEqual(set(discover_repositories([root])), {top, other_top})
            for depth, expected in (
                (0, {top, other_top}),
                (1, {top, other_top, first, sibling}),
                (2, {top, other_top, first, sibling, second}),
            ):
                with self.subTest(depth=depth):
                    self.assertEqual(set(discover_repositories([root], nested=depth)), expected)

    def test_pruning_at_depth_limit_skips_repo_contents_and_git_directories(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            child = top / "child"
            self._repo(top)
            self._repo(child)
            (child / "ordinary").mkdir()
            walk = os.walk
            visited: list[Path] = []

            def recording_walk(root: Path):
                for entry in walk(root):
                    visited.append(Path(entry[0]))
                    yield entry

            for depth, expected in ((0, [top]), (1, [top, child])):
                with self.subTest(depth=depth):
                    visited.clear()
                    with patch("gitoverit.reporting.os.walk", side_effect=recording_walk):
                        self.assertEqual(list(discover_repositories([top], nested=depth)), expected)
                    self.assertEqual(visited, expected)

    def test_ignored_repositories_are_included_within_depth_limit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            first = top / "vendor" / "first"
            second = first / "second"
            for path in (top, first, second):
                self._repo(path)
            (top / ".gitignore").write_text("vendor/\n")
            (first / ".gitignore").write_text("second/\n")

            for depth, expected in ((0, {top}), (1, {top, first}), (2, {top, first, second})):
                with self.subTest(depth=depth):
                    self.assertEqual(set(discover_repositories([top], nested=depth)), expected)

    def test_explicit_scan_roots_reset_depth(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            first = top / "ordinary" / "first"
            second = first / "second"
            for path in (top, first, second):
                self._repo(path)
            for root in (first, first.parent):
                with self.subTest(root=root):
                    self.assertEqual(list(discover_repositories([root])), [first])
                    self.assertEqual(set(discover_repositories([root], nested=1)), {first, second})

    def test_overlapping_roots_are_deduplicated_without_losing_depth(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            top = base / "top"
            first = top / "first"
            second = first / "second"
            for path in (top, first, second):
                self._repo(path)
            alias = base / "alias"
            alias.symlink_to(first, target_is_directory=True)
            for roots in ([top, first, alias], [alias, first, top]):
                for depth, expected in ((0, {top, first}), (1, {top, first, second})):
                    with self.subTest(roots=roots, depth=depth):
                        paths = list(discover_repositories(roots, nested=depth))
                        self.assertEqual(set(paths), expected)
                        self.assertEqual(len(paths), len(expected))

    def test_submodule_trees_are_excluded_even_as_explicit_roots(self) -> None:
        for style in ("absorbed", "gitlink", "declared"):
            with self.subTest(style=style), TemporaryDirectory() as tmpdir:
                top = Path(tmpdir).resolve() / "top"
                submodule = self._submodule(top, style)
                child = submodule / "ordinary" / "child"
                self._repo(child)
                for roots, expected in (
                    ([top], {top}),
                    ([submodule], set()),
                    ([child.parent], set()),
                    ([child], set()),
                    ([top, child], {top}),
                    ([child, top], {top}),
                ):
                    with self.subTest(roots=roots):
                        self.assertEqual(set(discover_repositories(roots, nested=3)), expected)

    def test_absorbed_submodule_is_excluded_without_parent_declarations(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            submodule = self._submodule(top, "absorbed")
            (top / ".gitmodules").unlink()
            Repo(top).git.update_index("--force-remove", submodule.relative_to(top).as_posix())
            self.assertEqual(list(discover_repositories([top], nested=3)), [top])
            self.assertEqual(list(discover_repositories([submodule], nested=3)), [])

    def test_declared_submodule_tree_without_checkout_is_excluded(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            parent = self._repo(top)
            submodule = top / "deps" / "missing checkout"
            child = submodule / "ordinary" / "child"
            self._repo(child)
            parent.git.config(
                "--file", str(top / ".gitmodules"), "submodule.example.path",
                submodule.relative_to(top).as_posix(),
            )
            self.assertEqual(list(discover_repositories([top], nested=2)), [top])
            for root in (submodule, child.parent, child):
                with self.subTest(root=root):
                    self.assertEqual(list(discover_repositories([root], nested=2)), [])

    def test_linked_worktrees_are_repositories_even_with_modules_in_the_path(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            main = self._repo(base / "modules" / "main", commit=True)
            top = base / "top"
            self._repo(top)
            worktree = top / "linked"
            main.git.worktree("add", "-b", "linked", str(worktree))
            self.assertEqual(list(discover_repositories([top])), [top])
            self.assertEqual(set(discover_repositories([top], nested=1)), {top, worktree})
            self.assertEqual(list(discover_repositories([worktree])), [worktree])

    def test_negative_depth_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nested"):
            list(discover_repositories([], nested=-1))

    def test_cli_depth_limit_in_sequential_and_parallel_modes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            top = Path(tmpdir).resolve() / "top"
            first = top / "first"
            second = first / "second"
            for path in (top, first, second):
                self._repo(path)
            for jobs in ("0", "2"):
                for options, expected in (
                    ([], {top}),
                    (["--nested", "1"], {top, first}),
                    (["--nested", "2"], {top, first, second}),
                ):
                    with self.subTest(jobs=jobs, options=options):
                        result = RUNNER.invoke(APP, [
                            str(top), "--jobs", jobs, "--metadata-only",
                            "--print", "path", "--no-progress", *options,
                        ])
                        self.assertEqual(result.exit_code, 0, result.output)
                        self.assertEqual(set(result.stdout.splitlines()), {str(p) for p in expected})
            reports = collect_reports([top], fetch=False, dirty_only=False, nested=1, metadata_only=True)
            self.assertEqual({report.path for report in reports}, {top, first})

    def test_cli_rejects_negative_depth_before_collection(self) -> None:
        with patch("gitoverit.repos_cli.collect_reports_parallel") as collect:
            result = RUNNER.invoke(APP, ["--nested", "-1"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--nested", result.output)
        collect.assert_not_called()

    def test_discovery_prunes_at_default_depth(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            Repo.init(parent)
            observed_dirnames: list[str] = []

            def fake_walk(root: Path):
                dirnames = ["ignored", "kept", ".git"]
                yield str(root), dirnames, []
                observed_dirnames.extend(dirnames)

            with patch("gitoverit.reporting.os.walk", side_effect=fake_walk):
                discovered = list(discover_repositories([parent]))

            self.assertEqual(discovered, [parent.resolve()])
            self.assertEqual(observed_dirnames, [])

    def test_gitignored_nested_repo_is_skipped(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            parent_repo = Repo.init(parent)

            # Create a .gitignore that ignores the nested dir
            gitignore = parent / ".gitignore"
            gitignore.write_text("nested/\n")
            parent_repo.index.add([".gitignore"])
            parent_repo.index.commit("init", author=AUTHOR, committer=AUTHOR)

            # Create a nested repo inside the gitignored directory
            nested = parent / "nested"
            nested.mkdir()
            Repo.init(nested)

            discovered = list(discover_repositories([parent]))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertNotIn(nested.resolve(), resolved_paths)

    def test_gitignored_nested_repo_is_found_with_nested(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            parent_repo = Repo.init(parent)

            gitignore = parent / ".gitignore"
            gitignore.write_text("nested/\n")
            parent_repo.index.add([".gitignore"])
            author = Actor("Tester", "tester@example.com")
            parent_repo.index.commit("init", author=author, committer=author)

            nested = parent / "nested"
            nested.mkdir()
            Repo.init(nested)

            discovered = list(discover_repositories([parent], nested=1))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertIn(nested.resolve(), resolved_paths)

    def test_non_gitignored_nested_repo_is_found(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            Repo.init(parent)

            # Nested repo that is NOT gitignored
            nested = parent / "nested"
            nested.mkdir()
            Repo.init(nested)

            discovered = list(discover_repositories([parent], nested=1))
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
            parent_repo.index.commit("init", author=AUTHOR, committer=AUTHOR)

            # Create vendor/lib which is a repo, under a gitignored path
            vendor_lib = parent / "vendor" / "lib"
            vendor_lib.mkdir(parents=True)
            Repo.init(vendor_lib)

            discovered = list(discover_repositories([parent]))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertNotIn(vendor_lib.resolve(), resolved_paths)

    def test_deeply_nested_under_gitignored_is_found_with_nested(self) -> None:
        with TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            parent_repo = Repo.init(parent)

            gitignore = parent / ".gitignore"
            gitignore.write_text("vendor/\n")
            parent_repo.index.add([".gitignore"])
            author = Actor("Tester", "tester@example.com")
            parent_repo.index.commit("init", author=author, committer=author)

            vendor_lib = parent / "vendor" / "lib"
            vendor_lib.mkdir(parents=True)
            Repo.init(vendor_lib)

            discovered = list(discover_repositories([parent], nested=1))
            resolved_paths = [p.resolve() for p in discovered]
            self.assertIn(parent.resolve(), resolved_paths)
            self.assertIn(vendor_lib.resolve(), resolved_paths)


class FilterReportsTests(unittest.TestCase):
    def test_mtime_available_in_where_expression(self) -> None:
        old_report = RepoReport(
            path=Path("/tmp/old"),
            display_path="old",
            fetch_failed=False,
            status_segments=[],
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=False,
            latest_mtime=1.0,
        )
        new_report = RepoReport(
            path=Path("/tmp/new"),
            display_path="new",
            fetch_failed=False,
            status_segments=[],
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=False,
            latest_mtime=100.0,
        )

        filtered = _filter_reports([old_report, new_report], "mtime > 10")
        self.assertEqual([report.display_path for report in filtered], ["new"])

    def _make_report(self, **kwargs: object) -> RepoReport:
        defaults: dict[str, object] = dict(
            path=Path("/tmp/repo"),
            display_path="repo",
            fetch_failed=False,
            status_segments=[],
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=False,
            latest_mtime=None,
        )
        defaults.update(kwargs)
        return RepoReport(**defaults)  # type: ignore[arg-type]

    def test_filter_ahead_behind(self) -> None:
        ahead_report = self._make_report(display_path="ahead", ahead=3)
        behind_report = self._make_report(display_path="behind", behind=2)
        clean_report = self._make_report(display_path="clean")

        filtered = _filter_reports(
            [ahead_report, behind_report, clean_report], "ahead > 0"
        )
        self.assertEqual([r.display_path for r in filtered], ["ahead"])

        filtered = _filter_reports(
            [ahead_report, behind_report, clean_report], "behind > 0"
        )
        self.assertEqual([r.display_path for r in filtered], ["behind"])

    def test_filter_modified_untracked_deleted(self) -> None:
        reports = [
            self._make_report(display_path="mod", modified=5),
            self._make_report(display_path="unt", untracked=3),
            self._make_report(display_path="del", deleted=1),
            self._make_report(display_path="clean"),
        ]

        filtered = _filter_reports(reports, "modified > 0")
        self.assertEqual([r.display_path for r in filtered], ["mod"])

        filtered = _filter_reports(reports, "untracked > 0")
        self.assertEqual([r.display_path for r in filtered], ["unt"])

        filtered = _filter_reports(reports, "deleted > 0")
        self.assertEqual([r.display_path for r in filtered], ["del"])

    def test_filter_path_variable(self) -> None:
        reports = [
            self._make_report(display_path="a", path=Path("/home/user/a")),
            self._make_report(display_path="b", path=Path("/home/user/b")),
        ]
        filtered = _filter_reports(reports, 'path.endswith("/a")')
        self.assertEqual([r.display_path for r in filtered], ["a"])

    def test_dirty_excludes_ahead_behind(self) -> None:
        """dirty should be False when only ahead/behind are set."""
        report = self._make_report(ahead=5, behind=2)
        self.assertFalse(report.dirty)


class SortReportsTests(unittest.TestCase):
    def _make_report(self, **kwargs: object) -> RepoReport:
        defaults: dict[str, object] = dict(
            path=Path("/tmp/repo"),
            display_path="repo",
            fetch_failed=False,
            status_segments=[],
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=False,
            latest_mtime=None,
        )
        defaults.update(kwargs)
        return RepoReport(**defaults)  # type: ignore[arg-type]

    def test_supported_sort_modes(self) -> None:
        reports = [
            self._make_report(
                display_path="bravo/repo-b",
                path=Path("/tmp/zulu/repo-b"),
                branch="main",
                remote="origin/main",
                remote_url="github.com/example/repo-b",
                ident="Zed <zed@example.com>",
                latest_mtime=20.0,
            ),
            self._make_report(
                display_path="alpha/repo-c",
                path=Path("/tmp/alpha/repo-c"),
                status_segments=[("2m", "yellow", "core")],
                branch="feature",
                remote="-",
                remote_url="-",
                ident="Amy <amy@example.com>",
                latest_mtime=10.0,
            ),
            self._make_report(
                display_path="charlie/repo-a",
                path=Path("/tmp/beta/repo-a"),
                status_segments=[("1u", "red", "core")],
                branch="dev",
                remote="upstream/dev",
                remote_url="example.com/repo-a",
                ident=None,
                latest_mtime=None,
            ),
        ]

        expected_orders = {
            SortMode.DIR: ["alpha/repo-c", "bravo/repo-b", "charlie/repo-a"],
            SortMode.PATH: ["alpha/repo-c", "charlie/repo-a", "bravo/repo-b"],
            SortMode.NAME: ["charlie/repo-a", "bravo/repo-b", "alpha/repo-c"],
            SortMode.STATUS: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
            SortMode.BRANCH_REMOTE: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
            SortMode.BRANCH: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
            SortMode.REMOTE: ["alpha/repo-c", "bravo/repo-b", "charlie/repo-a"],
            SortMode.URL: ["alpha/repo-c", "charlie/repo-a", "bravo/repo-b"],
            SortMode.MTIME: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
            SortMode.IDENT: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
            SortMode.AUTHOR: ["charlie/repo-a", "alpha/repo-c", "bravo/repo-b"],
        }

        self.assertEqual(
            set(expected_orders),
            set(SortMode) - {SortMode.NONE},
        )

        for sort_mode, expected in expected_orders.items():
            ordered = list(reports)
            _sort_reports(ordered, sort=sort_mode, reverse=False)
            self.assertEqual(
                [report.display_path for report in ordered],
                expected,
                msg=f"unexpected order for {sort_mode.value}",
            )

    def test_reverse_sort_applies_to_selected_mode(self) -> None:
        reports = [
            self._make_report(display_path="b", path=Path("/tmp/b")),
            self._make_report(display_path="a", path=Path("/tmp/a")),
            self._make_report(display_path="c", path=Path("/tmp/c")),
        ]

        _sort_reports(reports, sort=SortMode.DIR, reverse=True)
        self.assertEqual([report.display_path for report in reports], ["c", "b", "a"])

    def test_reverse_without_sort_reverses_current_order(self) -> None:
        reports = [
            self._make_report(display_path="first"),
            self._make_report(display_path="second"),
            self._make_report(display_path="third"),
        ]

        _sort_reports(reports, sort=SortMode.NONE, reverse=True)
        self.assertEqual(
            [report.display_path for report in reports],
            ["third", "second", "first"],
        )

class PullSafeTests(unittest.TestCase):
    def _commit_file(self, repo: Repo, relative_path: str, content: str, message: str) -> None:
        worktree = Path(repo.working_tree_dir or "")
        path = worktree / relative_path
        path.write_text(content)
        repo.index.add([relative_path])
        repo.index.commit(message, author=AUTHOR, committer=AUTHOR)

    def _clone_remote_triplet(self, tmpdir: str) -> tuple[Repo, Repo]:
        remote = Path(tmpdir) / "remote.git"
        Repo.init(remote, bare=True)

        seed = Repo.clone_from(str(remote), Path(tmpdir) / "seed")
        self._commit_file(seed, "README.md", "seed\n", "seed")
        seed.git.push("--set-upstream", "origin", seed.active_branch.name)

        local = Repo.clone_from(str(remote), Path(tmpdir) / "local")
        other = Repo.clone_from(str(remote), Path(tmpdir) / "other")
        return local, other

    def test_pull_safe_fast_forwards_clean_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            local, other = self._clone_remote_triplet(tmpdir)
            self._commit_file(other, "README.md", "remote change\n", "remote change")
            other.git.push("origin", other.active_branch.name)

            report = analyze_repository(Path(local.working_tree_dir or ""), fetch=False, pull_safe=True)

            self.assertTrue(report.pulled)
            self.assertFalse(report.pull_failed)
            self.assertEqual(report.behind, 0)
            self.assertFalse(report.dirty)
            self.assertIn(("P", "cyan", "extras"), report.status_segments)

            payload = json.loads(render_json([report]))[0]
            self.assertTrue(payload["pulled"])
            self.assertFalse(payload["pull_failed"])

    def test_pull_safe_skips_dirty_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            local, other = self._clone_remote_triplet(tmpdir)
            self._commit_file(other, "README.md", "remote change\n", "remote change")
            other.git.push("origin", other.active_branch.name)

            dirty_path = Path(local.working_tree_dir or "") / "local.txt"
            dirty_path.write_text("dirty\n")
            before = local.head.commit.hexsha

            report = analyze_repository(Path(local.working_tree_dir or ""), fetch=False, pull_safe=True)

            self.assertFalse(report.pulled)
            self.assertFalse(report.pull_failed)
            self.assertEqual(report.behind, 1)
            self.assertTrue(report.dirty)
            self.assertEqual(local.head.commit.hexsha, before)

    def test_pull_safe_skips_diverged_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            local, other = self._clone_remote_triplet(tmpdir)
            self._commit_file(other, "README.md", "remote change\n", "remote change")
            other.git.push("origin", other.active_branch.name)

            self._commit_file(local, "local.txt", "local change\n", "local change")
            before = local.head.commit.hexsha

            report = analyze_repository(Path(local.working_tree_dir or ""), fetch=False, pull_safe=True)

            self.assertFalse(report.pulled)
            self.assertFalse(report.pull_failed)
            self.assertEqual(report.ahead, 1)
            self.assertEqual(report.behind, 1)
            self.assertEqual(local.head.commit.hexsha, before)


class PushSafeTests(unittest.TestCase):
    def _commit_file(self, repo: Repo, relative_path: str, content: str, message: str) -> None:
        worktree = Path(repo.working_tree_dir or "")
        path = worktree / relative_path
        path.write_text(content)
        repo.index.add([relative_path])
        repo.index.commit(message, author=AUTHOR, committer=AUTHOR)

    def _clone_remote_triplet(self, tmpdir: str) -> tuple[Repo, Repo, Repo]:
        remote_path = Path(tmpdir) / "remote.git"
        remote = Repo.init(remote_path, bare=True)

        seed = Repo.clone_from(str(remote_path), Path(tmpdir) / "seed")
        self._commit_file(seed, "README.md", "seed\n", "seed")
        seed.git.push("--set-upstream", "origin", seed.active_branch.name)

        local = Repo.clone_from(str(remote_path), Path(tmpdir) / "local")
        other = Repo.clone_from(str(remote_path), Path(tmpdir) / "other")
        return remote, local, other

    def test_push_safe_pushes_clean_ahead_repo_to_exact_upstream(self) -> None:
        with TemporaryDirectory() as tmpdir:
            remote, local, _ = self._clone_remote_triplet(tmpdir)
            upstream_branch = local.active_branch.tracking_branch()
            self.assertIsNotNone(upstream_branch)
            assert upstream_branch is not None
            remote_branch = upstream_branch.remote_head

            local.git.branch("-m", "local-only")
            self._commit_file(local, "local.txt", "local change\n", "local change")

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                pull_safe=True,
                push_safe=True,
            )

            self.assertFalse(report.pulled)
            self.assertTrue(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.ahead, 0)
            self.assertEqual(report.behind, 0)
            self.assertFalse(report.dirty)
            self.assertIn(("U", "green", "extras"), report.status_segments)
            self.assertEqual(
                remote.commit(f"refs/heads/{remote_branch}").hexsha,
                local.head.commit.hexsha,
            )

            payload = json.loads(render_json([report]))[0]
            self.assertTrue(payload["pushed"])
            self.assertFalse(payload["push_failed"])

    def test_push_safe_skips_dirty_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            remote, local, _ = self._clone_remote_triplet(tmpdir)
            branch = local.active_branch.name
            remote_before = remote.commit(f"refs/heads/{branch}").hexsha
            self._commit_file(local, "local.txt", "committed\n", "local change")
            Path(local.working_tree_dir or "", "dirty.txt").write_text("dirty\n")

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                push_safe=True,
            )

            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.ahead, 1)
            self.assertTrue(report.dirty)
            self.assertEqual(remote.commit(f"refs/heads/{branch}").hexsha, remote_before)

    def test_push_safe_skips_diverged_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            remote, local, other = self._clone_remote_triplet(tmpdir)
            branch = local.active_branch.name
            self._commit_file(local, "local.txt", "local\n", "local change")
            self._commit_file(other, "remote.txt", "remote\n", "remote change")
            other.git.push("origin", other.active_branch.name)
            remote_before = remote.commit(f"refs/heads/{branch}").hexsha

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                push_safe=True,
            )

            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.ahead, 1)
            self.assertEqual(report.behind, 1)
            self.assertEqual(remote.commit(f"refs/heads/{branch}").hexsha, remote_before)

    def test_push_safe_skips_behind_only_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            _, local, other = self._clone_remote_triplet(tmpdir)
            self._commit_file(other, "remote.txt", "remote\n", "remote change")
            other.git.push("origin", other.active_branch.name)

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                push_safe=True,
            )

            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.ahead, 0)
            self.assertEqual(report.behind, 1)

    def test_combined_safe_modes_pull_behind_only_repo_without_push(self) -> None:
        with TemporaryDirectory() as tmpdir:
            _, local, other = self._clone_remote_triplet(tmpdir)
            self._commit_file(other, "remote.txt", "remote\n", "remote change")
            other.git.push("origin", other.active_branch.name)

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                pull_safe=True,
                push_safe=True,
            )

            self.assertTrue(report.pulled)
            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.ahead, 0)
            self.assertEqual(report.behind, 0)

    def test_push_safe_skips_detached_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            remote, local, _ = self._clone_remote_triplet(tmpdir)
            branch = local.active_branch.name
            remote_before = remote.commit(f"refs/heads/{branch}").hexsha
            local.git.checkout("--detach")
            self._commit_file(local, "local.txt", "local\n", "detached change")

            report = analyze_repository(
                Path(local.working_tree_dir or ""),
                fetch=False,
                push_safe=True,
            )

            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(remote.commit(f"refs/heads/{branch}").hexsha, remote_before)

    def test_push_safe_skips_repo_without_upstream(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = Repo.init(tmpdir)
            self._commit_file(repo, "local.txt", "local\n", "local change")

            report = analyze_repository(
                Path(tmpdir),
                fetch=False,
                push_safe=True,
            )

            self.assertFalse(report.pushed)
            self.assertFalse(report.push_failed)
            self.assertEqual(report.remote, "-")

    def test_push_safe_failure_is_reported_and_retained_by_dirty_only(self) -> None:
        with TemporaryDirectory() as tmpdir:
            _, local, _ = self._clone_remote_triplet(tmpdir)
            missing_push_remote = Path(tmpdir) / "missing.git"
            local.git.remote(
                "set-url",
                "--push",
                "origin",
                str(missing_push_remote),
            )
            self._commit_file(local, "local.txt", "local\n", "local change")

            reports = collect_reports(
                [Path(local.working_tree_dir or "")],
                fetch=False,
                dirty_only=True,
                push_safe=True,
            )

            self.assertEqual(len(reports), 1)
            report = reports[0]
            self.assertFalse(report.pushed)
            self.assertTrue(report.push_failed)
            self.assertEqual(report.ahead, 1)
            self.assertFalse(report.dirty)
            self.assertTrue(report.display_path.startswith("! "))
            self.assertNotIn(("U", "green", "extras"), report.status_segments)

            payload = json.loads(render_json([report]))[0]
            self.assertFalse(payload["pushed"])
            self.assertTrue(payload["push_failed"])


class PrintReportsTests(unittest.TestCase):
    def _make_report(self, **kwargs: object) -> RepoReport:
        defaults: dict[str, object] = dict(
            path=Path("/tmp/repo"),
            display_path="repo",
            fetch_failed=False,
            status_segments=[],
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=False,
            latest_mtime=None,
        )
        defaults.update(kwargs)
        return RepoReport(**defaults)  # type: ignore[arg-type]

    def test_print_path(self) -> None:
        reports = [
            self._make_report(path=Path("/home/user/a")),
            self._make_report(path=Path("/home/user/b")),
        ]
        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            _print_reports(reports, "path", null_delimited=False)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(buf.getvalue(), "/home/user/a\n/home/user/b\n")

    def test_print_columns(self) -> None:
        reports = [
            self._make_report(
                path=Path("/a"), display_path="a", branch="feature/a", ahead=2,
                status_segments=[("1m", "yellow", "core")],
            ),
            self._make_report(
                path=Path("/b"), display_path="b", branch="feature/b", ahead=0,
                status_segments=[("1u", "red", "core")],
            ),
        ]
        cases = [
            ("path, dir, status, branch", ["/a\ta\t1m\tfeature/a", "/b\tb\t1u\tfeature/b"]),
            ("(path, dir, status, branch)", ["/a\ta\t1m\tfeature/a", "/b\tb\t1u\tfeature/b"]),
            (
                'branch.replace("feature/", ""), "left,right", ahead',
                ["a\tleft,right\t2", "b\tleft,right\t0"],
            ),
            ('locals()["branch"], "path" in dir()', ["feature/a\tTrue", "feature/b\tTrue"]),
            ("path,", ["/a", "/b"]),
            ('path, "tab\\tnewline\\n"', ["/a\ttab\tnewline\n", "/b\ttab\tnewline\n"]),
        ]
        for expr, rows in cases:
            for null_delimited in (False, True):
                with self.subTest(expr=expr, null_delimited=null_delimited):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        _print_reports(reports, expr, null_delimited=null_delimited)
                    end = "\0" if null_delimited else "\n"
                    self.assertEqual(buf.getvalue(), "".join(row + end for row in rows))

    def test_print_columns_preserves_attribute_checks_without_partial_row(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(FeatureNotAvailable):
            _print_reports(
                [self._make_report()], "path, branch.__class__", null_delimited=False
            )
        self.assertEqual(buf.getvalue(), "")

    def test_print_null_delimited(self) -> None:
        reports = [
            self._make_report(path=Path("/a")),
            self._make_report(path=Path("/b")),
        ]
        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            _print_reports(reports, "path", null_delimited=True)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(buf.getvalue(), "/a\0/b\0")

    def test_print_compound_expression(self) -> None:
        reports = [
            self._make_report(
                path=Path("/repo"), branch="feature/x", display_path="repo"
            ),
        ]
        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            _print_reports(reports, 'branch + ":" + dir', null_delimited=False)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(buf.getvalue(), "feature/x:repo\n")


if __name__ == "__main__":
    unittest.main()
