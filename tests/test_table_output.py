import unittest
from pathlib import Path

from rich.console import Console

from gitoverit.output import render_table
from gitoverit.reporting import RepoReport


class TableKeyOutputTests(unittest.TestCase):
    def _make_report(self, *, status_segments, exceptional: bool = False) -> RepoReport:
        if exceptional:
            status_segments = [("!", "bold red"), *status_segments]
        return RepoReport(
            path=Path("/tmp/repo"),
            display_path="repo",
            fetch_failed=False,
            status_segments=status_segments,
            branch="main",
            remote="-",
            remote_url="-",
            ident=None,
            dirty=True,
            latest_mtime=None,
        )

    def test_main_key_always_rendered(self) -> None:
        console = Console(record=True, width=120)
        report = self._make_report(status_segments=[("1m", "yellow")])

        render_table(console, [report])
        output = console.export_text()

        self.assertIn("m\u00A0modified", output)
        self.assertIn("u\u00A0untracked", output)
        self.assertIn("d\u00A0deleted", output)
        self.assertIn("+/-\u00A0lines\u00A0added/removed", output)
        self.assertIn("↑\u00A0ahead", output)
        self.assertIn("↓\u00A0behind", output)
        self.assertIn("s\u00A0submodules", output)

    def test_exceptional_key_only_when_bang_present(self) -> None:
        console_no_bang = Console(record=True, width=80)
        report_clean = self._make_report(status_segments=[("1m", "yellow")])
        render_table(console_no_bang, [report_clean])
        output_no_bang = console_no_bang.export_text()
        self.assertNotIn("any of: conflicts", output_no_bang)

        console_bang = Console(record=True, width=80)
        report_bang = self._make_report(status_segments=[("1m", "yellow")], exceptional=True)
        render_table(console_bang, [report_bang])
        output_bang = console_bang.export_text()
        self.assertIn("any of: conflicts", output_bang)


if __name__ == "__main__":
    unittest.main()
