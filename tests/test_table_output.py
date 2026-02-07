import random
import unittest
from pathlib import Path

from rich.console import Console

from gitoverit.output import render_table
from gitoverit.output.table import AutoTable
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


class AutoTableTests(unittest.TestCase):
    """Tests for the AutoTable layout algorithm."""

    def setUp(self) -> None:
        # Seed random for deterministic tests
        random.seed(42)

    def _render_table(self, table: AutoTable, width: int) -> str:
        """Render a table to string at given width."""
        console = Console(record=True, width=width, force_terminal=True)
        console.print(table)
        return console.export_text()

    def test_basic_rendering(self) -> None:
        """Table renders with borders and content."""
        table = AutoTable(width="fill")
        table.add_column("Name")
        table.add_column("Value")
        table.add_row("foo", "bar")

        output = self._render_table(table, width=40)

        self.assertIn("Name", output)
        self.assertIn("Value", output)
        self.assertIn("foo", output)
        self.assertIn("bar", output)
        # Check borders present
        self.assertIn("┏", output)
        self.assertIn("┓", output)

    def test_headers_never_truncated_when_possible(self) -> None:
        """Headers are protected from truncation (min_width = header length)."""
        table = AutoTable(width="fill")
        table.add_column("LongHeader")
        table.add_column("X")
        table.add_row("a", "b")

        output = self._render_table(table, width=40)

        # Full header should appear
        self.assertIn("LongHeader", output)

    def test_single_line_per_row_enforced(self) -> None:
        """Each data row occupies exactly one line (no wrapping)."""
        table = AutoTable(width="fill")
        table.add_column("Col")
        table.add_row("This is a very long cell that would normally wrap")

        output = self._render_table(table, width=30)
        lines = output.strip().split("\n")

        # Should have: top border, header, separator, data row, bottom border = 5 lines
        # Count lines with actual cell content (│ character)
        data_lines = [line for line in lines if "│" in line and "Col" not in line]
        self.assertEqual(len(data_lines), 1, "Data should be on exactly one line")

    def test_abbreviation_with_ellipsis(self) -> None:
        """Long content is truncated with ellipsis."""
        table = AutoTable(width="fill")
        table.add_column("Col")
        table.add_row("ThisIsAVeryLongValueThatWillBeTruncated")

        output = self._render_table(table, width=20)

        self.assertIn("…", output)

    def test_pack_mode_no_abbreviation_when_fits(self) -> None:
        """Pack mode uses minimum width needed when content fits terminal."""
        table = AutoTable(width="pack")
        table.add_column("A")
        table.add_column("B")
        table.add_row("12", "34")

        output = self._render_table(table, width=80)

        # Should not have ellipsis - content fits
        self.assertNotIn("…", output)

    def test_fill_mode_expands_to_terminal(self) -> None:
        """Fill mode table chrome fills terminal width."""
        table = AutoTable(width="fill")
        table.add_column("A")
        table.add_row("x")

        output = self._render_table(table, width=60)
        lines = output.strip().split("\n")

        # Top border should be close to 60 chars (minus any trailing space stripping)
        top_border = lines[0]
        self.assertGreater(len(top_border), 50)

    def test_fixed_width_mode(self) -> None:
        """Fixed width mode respects exact width."""
        table = AutoTable(width=30)
        table.add_column("Column")
        table.add_row("data")

        output = self._render_table(table, width=100)
        lines = output.strip().split("\n")

        # Table should be ~30 chars wide, not 100
        top_border = lines[0]
        self.assertLessEqual(len(top_border), 32)  # Allow small variance

    def test_measure_columns_ideal_width(self) -> None:
        """Ideal width is max of header and all cell widths."""
        table = AutoTable()
        table.add_column("Head")  # 4 chars
        table.add_row("LongerContent")  # 13 chars
        table.add_row("Short")  # 5 chars

        ideal, _ = table._measure_columns()

        self.assertEqual(ideal[0], 13)  # Max cell width

    def test_measure_columns_min_width_is_header(self) -> None:
        """Minimum width is at least the header length."""
        table = AutoTable()
        table.add_column("Header")  # 6 chars
        table.add_row("x")  # 1 char

        _, min_widths = table._measure_columns()

        self.assertEqual(min_widths[0], 6)  # Header length

    def test_count_abbreviated_includes_headers(self) -> None:
        """Abbreviation count includes headers that would be truncated."""
        table = AutoTable()
        table.add_column("LongHeader")  # 10 chars
        table.add_row("short")

        counts = table._count_abbreviated([5])  # Width too small for header

        self.assertEqual(counts[0], 1)  # Header is abbreviated

    def test_optimization_reduces_total_abbreviation(self) -> None:
        """The optimization algorithm reduces total abbreviated cells."""
        table = AutoTable()
        # Column A: many short cells
        # Column B: one very long cell
        table.add_column("A")
        table.add_column("B")
        for _ in range(5):
            table.add_row("x", "ThisIsAVeryLongValue")

        ideal, min_widths = table._measure_columns()
        # Force narrow widths
        initial_widths = [10, 10]
        optimized = table._optimize_widths(initial_widths, min_widths)

        initial_abbr = sum(table._count_abbreviated(initial_widths))
        optimized_abbr = sum(table._count_abbreviated(optimized))

        self.assertLessEqual(optimized_abbr, initial_abbr)


if __name__ == "__main__":
    unittest.main()
