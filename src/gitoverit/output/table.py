from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

import rich._wrap as rich_wrap
import rich.box as box
from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..reporting import RepoReport


# Override Rich's word-break regex to treat non-breaking spaces as non-whitespace
rich_wrap.re_word = re.compile(r"[^\S\u00A0]*[\S\u00A0]+[^\S\u00A0]*")


WidthMode = Literal["fill", "pack"] | int


@dataclass
class AutoColumn:
    """Column definition for AutoTable."""

    header: str
    style: Style | str | None = None
    priority: int = 1  # Higher = more important, less likely to be truncated


@dataclass
class AutoTable:
    """A table that enforces single-line rows and minimizes abbreviated cells.

    Args:
        width: "fill" (terminal width), "pack" (minimum needed), or fixed int.
        box_style: Box style for borders.
        padding: Horizontal padding per cell (left + right total).
        min_col_width: Minimum width for any column.
        max_iterations: Maximum iterations for layout algorithm.
    """

    width: WidthMode = "fill"
    box_style: box.Box = box.HEAVY_HEAD
    padding: int = 2
    min_col_width: int = 4
    minimize_chars: bool = False  # False = minimize cells (strategic), True = minimize chars (spread)

    _columns: list[AutoColumn] = field(default_factory=list)
    _rows: list[list[Text | str]] = field(default_factory=list)

    def add_column(
        self, header: str, style: Style | str | None = None, priority: int = 1
    ) -> None:
        """Add a column to the table.

        Args:
            header: Column header text.
            style: Optional style for the column.
            priority: Importance weight (higher = protect from truncation).
        """
        self._columns.append(AutoColumn(header=header, style=style, priority=priority))

    def add_row(self, *cells: Text | str) -> None:
        """Add a row to the table."""
        self._rows.append(list(cells))

    def _measure_columns(self) -> tuple[list[int], list[int]]:
        """Measure ideal and minimum widths for each column.

        Returns:
            (ideal_widths, min_widths) - lists of widths per column.
        """
        num_cols = len(self._columns)
        ideal_widths = [0] * num_cols
        min_widths = [self.min_col_width] * num_cols

        # Measure headers - header width is the inviolable minimum
        for i, col in enumerate(self._columns):
            header_width = cell_len(col.header)
            ideal_widths[i] = max(ideal_widths[i], header_width)
            min_widths[i] = max(min_widths[i], header_width)

        # Measure all cells
        for row in self._rows:
            for i, cell in enumerate(row):
                if i >= num_cols:
                    break
                text = cell.plain if isinstance(cell, Text) else str(cell)
                width = cell_len(text)
                ideal_widths[i] = max(ideal_widths[i], width)

        return ideal_widths, min_widths

    def _count_abbreviated(self, widths: list[int]) -> list[int]:
        """Count how many cells in each column would be abbreviated at given widths.

        Includes headers in the count.
        """
        num_cols = len(self._columns)
        counts = [0] * num_cols

        # Count header abbreviation
        for i, col in enumerate(self._columns):
            if cell_len(col.header) > widths[i]:
                counts[i] += 1

        # Count data abbreviation
        for row in self._rows:
            for i, cell in enumerate(row):
                if i >= num_cols:
                    break
                text = cell.plain if isinstance(cell, Text) else str(cell)
                if cell_len(text) > widths[i]:
                    counts[i] += 1

        return counts

    def _calculate_chrome_width(self, num_cols: int) -> int:
        """Calculate width used by borders and padding."""
        # Borders: left edge + right edge + separators between columns
        border_width = 2 + (num_cols - 1) if self.box_style else 0
        # Padding per column
        padding_width = num_cols * self.padding
        return border_width + padding_width

    def _distribute_proportionally(
        self, available: int, ideal_widths: list[int], min_widths: list[int]
    ) -> list[int]:
        """Distribute available width proportionally by ideal widths."""
        total_ideal = sum(ideal_widths)
        if total_ideal == 0:
            # Edge case: all columns empty
            num_cols = len(ideal_widths)
            return [available // num_cols] * num_cols

        # Start with proportional allocation
        widths = []
        remaining = available
        for i, ideal in enumerate(ideal_widths):
            if i == len(ideal_widths) - 1:
                # Last column gets remainder
                w = remaining
            else:
                w = int(available * ideal / total_ideal)
            w = max(w, min_widths[i])
            widths.append(w)
            remaining -= w

        # Adjust if we exceeded available (due to min_widths)
        total = sum(widths)
        while total > available:
            # Find column with most excess over minimum
            excess = [(i, widths[i] - min_widths[i]) for i in range(len(widths))]
            excess = [(i, e) for i, e in excess if e > 0]
            if not excess:
                break
            excess.sort(key=lambda x: x[1], reverse=True)
            idx = excess[0][0]
            widths[idx] -= 1
            total -= 1

        return widths

    def _get_cell_widths(self, col_idx: int) -> list[int]:
        """Get the widths of all cells in a column (including header)."""
        widths = [cell_len(self._columns[col_idx].header)]
        for row in self._rows:
            if col_idx < len(row):
                cell = row[col_idx]
                text = cell.plain if isinstance(cell, Text) else str(cell)
                widths.append(cell_len(text))
        return widths

    def _count_truncated_chars(self, widths: list[int]) -> int:
        """Total characters truncated across all cells."""
        total = 0
        for col_idx, width in enumerate(widths):
            for cell_width in self._get_cell_widths(col_idx):
                if cell_width > width:
                    total += cell_width - width
        return total

    def _marginal_benefit_chars(self, col_idx: int, current_width: int) -> int:
        """How many characters of truncation would be saved by +1 width?

        Equal to the count of cells still abbreviated (each saves 1 char).
        """
        cell_widths = self._get_cell_widths(col_idx)
        return sum(1 for w in cell_widths if w > current_width)

    def _optimize_widths_greedy(
        self,
        available: int,
        min_widths: list[int],
        minimize_chars: bool = True,
    ) -> list[int]:
        """Greedy optimal algorithm for width allocation.

        Args:
            available: Total width available for content.
            min_widths: Minimum width for each column.
            minimize_chars: If True, minimize total characters truncated.
                           If False, minimize count of truncated cells.
        """
        if minimize_chars:
            return self._optimize_greedy_chars(available, min_widths)
        else:
            return self._optimize_greedy_cells(available, min_widths)

    def _optimize_greedy_chars(self, available: int, min_widths: list[int]) -> list[int]:
        """Minimize total (priority-weighted) characters truncated.

        Greedy: give +1 to whichever column has the highest weighted benefit
        (abbreviated cells * priority).
        """
        num_cols = len(min_widths)
        widths = list(min_widths)
        budget = available - sum(min_widths)

        if budget <= 0:
            return widths

        # Precompute all cell widths and priorities
        all_cell_widths = [self._get_cell_widths(i) for i in range(num_cols)]
        priorities = [self._columns[i].priority for i in range(num_cols)]

        for _ in range(budget):
            best_col = -1
            best_benefit = 0

            for col_idx, cell_widths in enumerate(all_cell_widths):
                # Benefit = count of cells still abbreviated * priority
                raw_benefit = sum(1 for w in cell_widths if w > widths[col_idx])
                benefit = raw_benefit * priorities[col_idx]
                if benefit > best_benefit:
                    best_benefit = benefit
                    best_col = col_idx

            if best_col == -1 or best_benefit == 0:
                break  # Nothing more to improve

            widths[best_col] += 1

        return widths

    def _optimize_greedy_cells(self, available: int, min_widths: list[int]) -> list[int]:
        """Minimize count of (priority-weighted) truncated cells.

        Greedy: jump to breakpoints (cell widths), picking best cost/benefit ratio.
        Priority is factored into the benefit calculation.
        """
        num_cols = len(min_widths)
        widths = list(min_widths)
        budget = available - sum(min_widths)

        if budget <= 0:
            return widths

        # Precompute all cell widths and priorities
        all_cell_widths = [self._get_cell_widths(i) for i in range(num_cols)]
        priorities = [self._columns[i].priority for i in range(num_cols)]

        while budget > 0:
            best_col = -1
            best_ratio = 0.0  # weighted benefit per unit cost
            best_cost = 0
            best_benefit = 0

            for col_idx, cell_widths in enumerate(all_cell_widths):
                current = widths[col_idx]
                # Find the next breakpoint: smallest cell width > current
                abbreviated = [w for w in cell_widths if w > current]
                if not abbreviated:
                    continue  # All cells fit, no benefit

                next_breakpoint = min(abbreviated)
                cost = next_breakpoint - current

                if cost > budget:
                    continue  # Can't afford this jump

                # Benefit: cells un-abbreviated * priority
                raw_benefit = sum(1 for w in cell_widths if w == next_breakpoint)
                benefit = raw_benefit * priorities[col_idx]

                # Ratio: weighted benefit per unit of width spent
                ratio = benefit / cost if cost > 0 else 0

                if ratio > best_ratio or (ratio == best_ratio and benefit > best_benefit):
                    best_ratio = ratio
                    best_col = col_idx
                    best_cost = cost
                    best_benefit = benefit

            if best_col == -1:
                # No affordable improvements, distribute remainder to abbreviated cols
                abbreviated_cols = [
                    i for i in range(num_cols)
                    if any(w > widths[i] for w in all_cell_widths[i])
                ]
                if abbreviated_cols:
                    for i in range(budget):
                        widths[abbreviated_cols[i % len(abbreviated_cols)]] += 1
                break

            widths[best_col] += best_cost
            budget -= best_cost

        return widths

    def _donor_capacity(self, col_idx: int, current_width: int, min_width: int) -> int:
        """How much can this column donate without abbreviating any of its cells?

        Returns the amount the column can shrink before its widest
        currently-unabbreviated cell would become abbreviated.
        """
        cell_widths = self._get_cell_widths(col_idx)
        # Find the widest cell that currently fits
        fitting = [w for w in cell_widths if w <= current_width]
        if not fitting:
            # All cells already abbreviated, can shrink to min
            return current_width - min_width
        widest_fitting = max(fitting)
        # Can shrink down to widest_fitting (but not below min_width)
        return max(0, current_width - max(widest_fitting, min_width))

    def _receiver_need(self, col_idx: int, current_width: int) -> int | None:
        """How much does this column need to unabbreviate its shortest abbreviated cell?

        Returns None if no cells are abbreviated.
        """
        cell_widths = self._get_cell_widths(col_idx)
        abbreviated = [w for w in cell_widths if w > current_width]
        if not abbreviated:
            return None
        shortest_abbreviated = min(abbreviated)
        return shortest_abbreviated - current_width

    def _optimize_widths(
        self,
        widths: list[int],
        min_widths: list[int],
        max_iterations: int = 100,
        patience: int = 10,
    ) -> list[int]:
        """Iteratively reallocate widths to minimize total abbreviated cells."""
        widths = list(widths)  # Don't mutate input
        num_cols = len(widths)

        # Track best solution
        best_widths = list(widths)
        best_total = sum(self._count_abbreviated(widths))
        no_improvement_count = 0

        for _ in range(max_iterations):
            abbr_counts = self._count_abbreviated(widths)
            current_total = sum(abbr_counts)

            if current_total == 0:
                return widths  # Perfect, nothing to improve

            # Update best if current is better
            if current_total < best_total:
                best_total = current_total
                best_widths = list(widths)
                no_improvement_count = 0
            else:
                no_improvement_count += 1
                if no_improvement_count >= patience:
                    break

            # Get donors (sorted by abbr count ascending) and receivers (descending)
            donors = sorted(range(num_cols), key=lambda i: abbr_counts[i])
            donors = [i for i in donors if widths[i] > min_widths[i]]
            receivers = sorted(range(num_cols), key=lambda i: abbr_counts[i], reverse=True)

            if not donors:
                break

            found_improvement = False

            for receiver in receivers:
                needed = self._receiver_need(receiver, widths[receiver])
                if needed is None or needed <= 0:
                    continue

                # Accumulate donations from multiple donors if needed
                test_widths = list(widths)
                remaining_need = needed

                for donor in donors:
                    if donor == receiver:
                        continue

                    capacity = self._donor_capacity(donor, test_widths[donor], min_widths[donor])
                    if capacity <= 0:
                        continue

                    transfer = min(remaining_need, capacity)
                    test_widths[donor] -= transfer
                    test_widths[receiver] += transfer
                    remaining_need -= transfer

                    if remaining_need <= 0:
                        break

                # Check if this helped
                new_counts = self._count_abbreviated(test_widths)
                new_total = sum(new_counts)

                if new_total < current_total:
                    widths = test_widths
                    found_improvement = True
                    break

            # If no improving move found, try a random shake
            if not found_improvement and donors and len(receivers) > 1:
                donor = random.choice(donors)
                receiver = random.choice([r for r in receivers if r != donor])
                capacity = self._donor_capacity(donor, widths[donor], min_widths[donor])
                if capacity > 0:
                    transfer = random.randint(1, capacity)
                    widths[donor] -= transfer
                    widths[receiver] += transfer

        return best_widths

    def _calculate_layout(self, console_width: int) -> list[int]:
        """Calculate final column widths."""
        num_cols = len(self._columns)
        if num_cols == 0:
            return []

        ideal_widths, min_widths = self._measure_columns()
        chrome_width = self._calculate_chrome_width(num_cols)
        total_ideal = sum(ideal_widths) + chrome_width

        # Determine available width and whether to expand
        if isinstance(self.width, int):
            available_total = self.width
        elif self.width == "pack":
            if total_ideal <= console_width:
                return ideal_widths  # Fits without abbreviation, use packed size
            available_total = console_width
        else:  # "fill"
            available_total = console_width

        available_for_content = available_total - chrome_width

        # Check if ideal widths fit
        total_ideal_content = sum(ideal_widths)
        if total_ideal_content <= available_for_content:
            # Content fits - for "fill" mode, distribute extra space proportionally
            if self.width == "fill" and available_for_content > total_ideal_content:
                extra = available_for_content - total_ideal_content
                # Distribute extra space proportionally by ideal width
                widths = list(ideal_widths)
                for i in range(extra):
                    # Give to column with largest ideal width (round-robin through proportions)
                    idx = i % num_cols
                    widths[idx] += 1
                return widths
            return ideal_widths

        # Need abbreviation - use greedy optimal allocation
        widths = self._optimize_widths_greedy(
            available_for_content, min_widths, minimize_chars=self.minimize_chars
        )

        return widths

    def _truncate_cell(self, cell: Text | str, width: int) -> Text:
        """Truncate a cell to fit within width, adding ellipsis if needed."""
        if isinstance(cell, Text):
            text = cell.copy()
        else:
            text = Text(str(cell))

        if cell_len(text.plain) > width:
            text.truncate(width, overflow="ellipsis")

        return text

    def _render_row(
        self, cells: list[Text | str], widths: list[int], is_header: bool = False
    ) -> Iterable[Segment]:
        """Render a single row of the table."""
        # Left border
        if self.box_style:
            border_char = self.box_style.head_left if is_header else self.box_style.mid_left
            yield Segment(border_char)

        pad_left = self.padding // 2
        pad_right = self.padding - pad_left

        for i, (cell, width) in enumerate(zip(cells, widths)):
            # Left padding
            yield Segment(" " * pad_left)

            # Cell content
            text = self._truncate_cell(cell, width)
            content = text.plain
            content_width = cell_len(content)

            # Get style
            style = None
            if isinstance(cell, Text) and cell._spans:
                # Use the text's style
                pass  # Will be handled by yielding Text segments
            elif is_header:
                style = Style(bold=True)

            if isinstance(cell, Text):
                # Yield styled segments from Text object
                for seg in text.render(Console()):
                    yield seg
            else:
                yield Segment(content, style)

            # Pad to width
            padding_needed = width - content_width
            if padding_needed > 0:
                yield Segment(" " * padding_needed)

            # Right padding
            yield Segment(" " * pad_right)

            # Column separator or right border
            if self.box_style:
                if i < len(widths) - 1:
                    sep = self.box_style.head_vertical if is_header else self.box_style.mid_vertical
                    yield Segment(sep)
                else:
                    border = self.box_style.head_right if is_header else self.box_style.mid_right
                    yield Segment(border)

        yield Segment("\n")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the table."""
        if not self._columns:
            return

        content_widths = self._calculate_layout(options.max_width)
        # Box methods expect widths including padding
        box_widths = [w + self.padding for w in content_widths]

        # Top border
        if self.box_style:
            yield Segment(self.box_style.get_top(box_widths))
            yield Segment("\n")

        # Header row
        headers = [Text(col.header, style="bold") for col in self._columns]
        yield from self._render_row(headers, content_widths, is_header=True)

        # Header separator
        if self.box_style:
            yield Segment(self.box_style.get_row(box_widths, level="head"))
            yield Segment("\n")

        # Data rows
        for row in self._rows:
            yield from self._render_row(row, content_widths)

        # Bottom border
        if self.box_style:
            yield Segment(self.box_style.get_bottom(box_widths))
            yield Segment("\n")

NBSP = "\u00A0"


def _status_key_main() -> Text:
    text = Text("  Status key: ")
    first = True

    def add(symbol: str, description: str, style: str | None) -> None:
        nonlocal first
        if not first:
            text.append("  ")
        if style:
            text.append(symbol, style=style)
        else:
            text.append(symbol)
        text.append(NBSP)
        text.append(description)
        first = False

    add("m", "modified", "yellow")
    add("u", "untracked", "magenta")
    add("d", "deleted", "red")
    add("+/-", f"lines{NBSP}added/removed", "cyan")
    add("↑", "ahead", "green")
    add("↓", "behind", "bright_black")
    add("s", "submodules", "blue")

    return text


def _status_key_exceptional() -> Text:
    text = Text("              ")
    text.append("!", style="bold red")
    text.append(" ")
    text.append(
        "any of: conflicts, detached HEAD, in-progress/unfinished operation "
        "(merge, rebase, cherry-pick, etc.)"
    )
    return text


def _status_text(report: RepoReport) -> Text:
    if not report.status_segments:
        return Text("clean", style="green")
    text = Text()
    for idx, (value, style) in enumerate(report.status_segments):
        if idx:
            text.append(" ")
        text.append(value, style=style)
    return text


def render_table(
    console: Console, reports: Sequence[RepoReport], *, minimize_chars: bool = False
) -> None:
    show_exceptional_key = any(
        any(segment == "!" for segment, _ in report.status_segments) for report in reports
    )

    table = AutoTable(width="fill", minimize_chars=minimize_chars)
    table.add_column("Dir", priority=5)
    table.add_column("Status", priority=10)  # Most important: is it dirty?
    table.add_column("Branch", priority=5)
    table.add_column("Remote", priority=3)
    table.add_column("URL", priority=3)
    table.add_column("Ident", priority=1)  # Often predictable/same person

    for report in reports:
        table.add_row(
            report.display_path,
            _status_text(report),
            report.branch,
            report.remote,
            report.remote_url,
            report.ident or "-",
        )
    console.print(table)

    if reports:
        console.print(_status_key_main())
        if show_exceptional_key:
            console.print(_status_key_exceptional())


__all__ = ["AutoColumn", "AutoTable", "render_table"]
