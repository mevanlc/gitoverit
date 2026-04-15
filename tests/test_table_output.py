import random
import unittest
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

from gitoverit.output import render_table
from gitoverit.output.table import (
    AutoTable,
    ResponsiveCell,
    _as_responsive,
    _branch_remote_cell,
    _mtime_cell,
    _status_cell,
    _url_cell,
)
from gitoverit.reporting import RepoReport


class TableKeyOutputTests(unittest.TestCase):
    def _make_report(self, *, status_segments, exceptional: bool = False) -> RepoReport:
        if exceptional:
            status_segments = [("!", "bold red", "core"), *status_segments]
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
        report = self._make_report(status_segments=[("1m", "yellow", "core")])

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
        report_clean = self._make_report(status_segments=[("1m", "yellow", "core")])
        render_table(console_no_bang, [report_clean])
        output_no_bang = console_no_bang.export_text()
        self.assertNotIn("any of: conflicts", output_no_bang)

        console_bang = Console(record=True, width=80)
        report_bang = self._make_report(status_segments=[("1m", "yellow", "core")], exceptional=True)
        render_table(console_bang, [report_bang])
        output_bang = console_bang.export_text()
        self.assertIn("any of: conflicts", output_bang)

    def test_default_columns_use_branch_remote_and_mtime(self) -> None:
        console = Console(record=True, width=120)
        report = self._make_report(status_segments=[("1m", "yellow", "core")])

        render_table(console, [report])
        output = console.export_text()

        self.assertIn("Branch:Remote", output)
        self.assertIn("main:-", output)
        self.assertIn("Modified", output)


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


class ResponsiveCellTests(unittest.TestCase):
    def test_render_at_picks_widest_fitting(self) -> None:
        cell = ResponsiveCell(variants=(Text("abcdefgh"), Text("abcd"), Text("ab")))
        self.assertEqual(cell.render_at(100).plain, "abcdefgh")
        self.assertEqual(cell.render_at(8).plain, "abcdefgh")
        self.assertEqual(cell.render_at(7).plain, "abcd")
        self.assertEqual(cell.render_at(3).plain, "ab")

    def test_render_at_falls_back_to_narrowest(self) -> None:
        cell = ResponsiveCell(variants=(Text("hello"),))
        # At width 2 the only variant is too wide; cell returns it and lets
        # the layout deal with ellipsis truncation.
        self.assertEqual(cell.render_at(2).plain, "hello")

    def test_effective_width_matches_rendered_variant(self) -> None:
        cell = ResponsiveCell(variants=(Text("a" * 10), Text("a" * 4)))
        self.assertEqual(cell.effective_width(10), 10)
        self.assertEqual(cell.effective_width(7), 4)

    def test_as_responsive_wraps_plain_values(self) -> None:
        self.assertEqual(_as_responsive("hi").variants[0].plain, "hi")
        txt = Text("styled", style="red")
        self.assertIs(_as_responsive(txt).variants[0], txt)
        rc = ResponsiveCell(variants=(Text("x"),))
        self.assertIs(_as_responsive(rc), rc)

    def test_empty_variants_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResponsiveCell(variants=())


class ColumnLadderTests(unittest.TestCase):
    def _make_report(self, **overrides) -> RepoReport:
        defaults = dict(
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
        defaults.update(overrides)
        return RepoReport(**defaults)

    def test_status_cell_drops_plus_minus_then_extras(self) -> None:
        segments = [
            ("1m", "yellow", "core"),
            ("(+108/-77)", "cyan", "plus_minus"),
            ("1u", "magenta", "core"),
            ("2s", "blue", "extras"),
        ]
        report = self._make_report(status_segments=segments)
        cell = _status_cell(report)
        plains = [v.plain for v in cell.variants]
        self.assertEqual(plains[0], "1m (+108/-77) 1u 2s")
        self.assertEqual(plains[1], "1m 1u 2s")
        self.assertEqual(plains[2], "1m 1u")

    def test_status_cell_clean_has_single_variant(self) -> None:
        report = self._make_report(status_segments=[])
        cell = _status_cell(report)
        self.assertEqual(len(cell.variants), 1)
        self.assertEqual(cell.variants[0].plain, "clean")

    def test_mtime_cell_four_variants(self) -> None:
        ts = datetime(2026, 2, 12, 6, 21).timestamp()
        cell = _mtime_cell(ts)
        plains = [v.plain for v in cell.variants]
        self.assertEqual(plains, ["2026-02-12 06:21", "2026-02-12", "02-12", "0212"])

    def test_mtime_cell_none(self) -> None:
        cell = _mtime_cell(None)
        self.assertEqual([v.plain for v in cell.variants], ["-"])

    def test_branch_remote_ladder(self) -> None:
        cell = _branch_remote_cell("master", "origin/master")
        plains = [v.plain for v in cell.variants]
        self.assertEqual(plains, ["master:origin/master", "master:origin", "master:…"])

    def test_branch_remote_no_slash_in_remote(self) -> None:
        cell = _branch_remote_cell("main", "origin")
        plains = [v.plain for v in cell.variants]
        self.assertEqual(plains, ["main:origin", "main:…"])

    def test_url_ladder(self) -> None:
        cell = _url_cell("termux/termux-api-package")
        plains = [v.plain for v in cell.variants]
        self.assertEqual(plains, ["termux/termux-api-package", "termux/…"])

    def test_url_no_slash(self) -> None:
        cell = _url_cell("-")
        self.assertEqual([v.plain for v in cell.variants], ["-"])


class NegotiatedLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        random.seed(42)

    def _render_table(self, table: AutoTable, width: int) -> str:
        console = Console(record=True, width=width, force_terminal=True)
        console.print(table)
        return console.export_text()

    def test_responsive_cell_uses_wide_variant_when_space_allows(self) -> None:
        table = AutoTable(width="fill")
        table.add_column("Time")
        table.add_row(ResponsiveCell(variants=(
            Text("2026-02-12 06:21"), Text("02-12"),
        )))
        output = self._render_table(table, width=40)
        self.assertIn("2026-02-12 06:21", output)
        self.assertNotIn("02-12 ", output.replace("2026-02-12 06:21", ""))

    def test_responsive_cell_degrades_to_narrow_variant(self) -> None:
        table = AutoTable(width="fill")
        table.add_column("T")
        table.add_row(ResponsiveCell(variants=(
            Text("2026-02-12 06:21"), Text("02-12"),
        )))
        output = self._render_table(table, width=12)
        # Narrowed to the short variant, no ellipsis needed.
        self.assertIn("02-12", output)
        self.assertNotIn("…", output)

    def test_header_variant_is_used_at_narrow_widths(self) -> None:
        table = AutoTable(width="fill")
        table.add_column(ResponsiveCell(variants=(Text("Modified"), Text("Mtime"))))
        table.add_row(ResponsiveCell(variants=(Text("0212"),)))
        # At width 9 the full "Modified" (8) + chrome (4) can't fit; layout
        # narrows the header to "Mtime".
        output = self._render_table(table, width=9)
        self.assertIn("Mtime", output)
        self.assertNotIn("Modified", output)

    def test_plain_str_cell_still_ellipsis_truncates(self) -> None:
        """Single-variant cells still fall back to ellipsis when narrowed."""
        table = AutoTable(width="fill")
        table.add_column("X")
        table.add_row("ThisIsAVeryLongValue")
        output = self._render_table(table, width=14)
        self.assertIn("…", output)

    def test_effective_priority_bump(self) -> None:
        """A column that has already narrowed further should weigh more."""
        table = AutoTable(width="fill")
        table.add_column("A")
        table.add_row(ResponsiveCell(variants=(
            Text("aaaaaaaaaa"),  # 10
            Text("aa"),           # 2
        )))
        # Column "A" at width 2: one step below its widest variant.
        at_narrow = table._effective_priority(0, current_width=2)
        at_widest = table._effective_priority(0, current_width=10)
        self.assertGreater(at_narrow, at_widest)


if __name__ == "__main__":
    unittest.main()
