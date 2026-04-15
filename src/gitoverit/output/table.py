from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Literal, Sequence

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


@dataclass(frozen=True)
class ResponsiveCell:
    """A cell that describes how it prefers to shorten itself when space is tight.

    `variants` is an ordered tuple of pre-rendered Texts, widest → narrowest.
    Layout picks the widest variant that fits the allocated width; if even the
    narrowest is wider than the allocation, layout falls back to ellipsis-
    truncating it.

    `refuses` records the cell's preference when no variant fits:
    - "truncate" (default) — let the layout ellipsis-truncate the narrowest.
    - "drop"              — prefer to be dropped entirely. Recorded only;
                            column dropping is intentionally unimplemented for
                            now (tech debt — see plan).
    """

    variants: tuple[Text, ...]
    refuses: str = "truncate"

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError("ResponsiveCell needs at least one variant")
        object.__setattr__(
            self, "_widths", tuple(cell_len(v.plain) for v in self.variants)
        )

    @property
    def widths(self) -> tuple[int, ...]:
        return self._widths  # type: ignore[attr-defined]

    @property
    def max_width(self) -> int:
        return self._widths[0]  # type: ignore[attr-defined]

    @property
    def min_width(self) -> int:
        return self._widths[-1]  # type: ignore[attr-defined]

    def render_at(self, width: int) -> Text:
        """Widest variant whose width is <= `width`. Falls back to narrowest."""
        for variant, w in zip(self.variants, self._widths):  # type: ignore[attr-defined]
            if w <= width:
                return variant
        return self.variants[-1]

    def effective_width(self, width: int) -> int:
        """Width of content actually displayed at the given allocation.

        If the widest fitting variant is wider than `width` (i.e. even the
        narrowest variant overflows), layout ellipsis-truncates to `width` —
        so the displayed width is `width`, not the variant's natural width.
        Getting this right matters: the optimizer uses the delta of
        effective_width across breakpoints as the "benefit" of allocating
        more space. If this function reported the natural width, single-
        variant cells would appear to gain nothing from widening.
        """
        for w in self._widths:  # type: ignore[attr-defined]
            if w <= width:
                return w
        return min(self._widths[-1], width)  # type: ignore[attr-defined]


def _as_responsive(value: "ResponsiveCell | Text | str") -> ResponsiveCell:
    """Wrap plain str/Text cells as single-variant ResponsiveCells.

    Single-variant cells render identically to the pre-responsive behaviour.
    """
    if isinstance(value, ResponsiveCell):
        return value
    if isinstance(value, Text):
        return ResponsiveCell(variants=(value,))
    return ResponsiveCell(variants=(Text(str(value)),))


@dataclass
class AutoColumn:
    """Column definition for AutoTable."""

    header: ResponsiveCell
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
    _rows: list[list[ResponsiveCell]] = field(default_factory=list)

    def add_column(
        self,
        header: "ResponsiveCell | Text | str",
        style: Style | str | None = None,
        priority: int = 1,
    ) -> None:
        """Add a column to the table.

        Args:
            header: Column header (plain str/Text or a ResponsiveCell ladder).
            style: Optional style for the column.
            priority: Importance weight (higher = protect from truncation).
        """
        self._columns.append(
            AutoColumn(header=_as_responsive(header), style=style, priority=priority)
        )

    def add_row(self, *cells: "ResponsiveCell | Text | str") -> None:
        """Add a row to the table. Cells may be plain str/Text or ResponsiveCell."""
        self._rows.append([_as_responsive(c) for c in cells])

    def _column_cells(self, col_idx: int) -> list[ResponsiveCell]:
        """All ResponsiveCells in a column, header first."""
        cells: list[ResponsiveCell] = [self._columns[col_idx].header]
        for row in self._rows:
            if col_idx < len(row):
                cells.append(row[col_idx])
        return cells

    def _measure_columns(self) -> tuple[list[int], list[int]]:
        """Measure ideal and minimum widths for each column.

        Ideal = widest variant across cells + header.
        Min   = the hard floor below which the optimizer won't allocate. That's
                the header's narrowest variant, floored at `self.min_col_width`.
                Data cells below their narrowest variant get ellipsis-truncated
                — we trust them not to ask for more floor than the header does.
                (Responsive data cells still influence allocation via their
                variant widths, which the optimizer treats as breakpoints.)
        """
        num_cols = len(self._columns)
        ideal_widths = [0] * num_cols
        min_widths = [self.min_col_width] * num_cols

        for i, col in enumerate(self._columns):
            min_widths[i] = max(min_widths[i], col.header.min_width)
            for cell in self._column_cells(i):
                ideal_widths[i] = max(ideal_widths[i], cell.max_width)

        return ideal_widths, min_widths

    def _count_abbreviated(self, widths: list[int]) -> list[int]:
        """Count cells per column not rendering at their widest variant.

        Includes headers. A cell is "abbreviated" iff the allocated width is
        smaller than its widest variant.
        """
        num_cols = len(self._columns)
        counts = [0] * num_cols
        for i in range(num_cols):
            for cell in self._column_cells(i):
                if cell.max_width > widths[i]:
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

    def _count_truncated_chars(self, widths: list[int]) -> int:
        """Total chars hidden across all cells (max_width - effective_width)."""
        total = 0
        for col_idx, width in enumerate(widths):
            for cell in self._column_cells(col_idx):
                total += max(0, cell.max_width - cell.effective_width(width))
        return total

    def _column_breakpoints(self, col_idx: int, above: int = 0) -> list[int]:
        """Sorted unique variant widths across cells in this column, strictly above `above`."""
        seen: set[int] = set()
        for cell in self._column_cells(col_idx):
            for w in cell.widths:
                if w > above:
                    seen.add(w)
        return sorted(seen)

    def _effective_priority(self, col_idx: int, current_width: int) -> float:
        """Base priority bumped by how many variant steps the column has shed.

        A column that's already narrowed further weighs more, so the optimizer
        prefers giving width back to it over further narrowing a healthier
        column. Linear bump: `base * (1 + steps_down)`.
        """
        base = self._columns[col_idx].priority
        steps = len(self._column_breakpoints(col_idx, above=current_width))
        return base * (1 + steps)

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
        """Minimize total (priority-weighted) characters hidden.

        Greedy: give +1 to whichever column has the highest weighted benefit.
        Benefit = cells not yet rendering at their widest variant, weighted by
        effective priority (which bumps with how narrow the column already is).
        """
        num_cols = len(min_widths)
        widths = list(min_widths)
        budget = available - sum(min_widths)

        if budget <= 0:
            return widths

        column_cells = [self._column_cells(i) for i in range(num_cols)]

        for _ in range(budget):
            best_col = -1
            best_benefit = 0.0

            for col_idx, cells in enumerate(column_cells):
                raw_benefit = sum(1 for cell in cells if cell.max_width > widths[col_idx])
                if raw_benefit == 0:
                    continue
                benefit = raw_benefit * self._effective_priority(col_idx, widths[col_idx])
                if benefit > best_benefit:
                    best_benefit = benefit
                    best_col = col_idx

            if best_col == -1 or best_benefit == 0:
                break

            widths[best_col] += 1

        return widths

    def _optimize_greedy_cells(self, available: int, min_widths: list[int]) -> list[int]:
        """Minimize count of (priority-weighted) truncated cells.

        Greedy: jump to the next variant breakpoint, picking best benefit/cost
        ratio. Benefit = cells that jump to a wider variant at that breakpoint,
        weighted by effective priority. Variants let columns step through
        multiple breakpoints instead of the old single-width-per-cell model.
        """
        num_cols = len(min_widths)
        widths = list(min_widths)
        budget = available - sum(min_widths)

        if budget <= 0:
            return widths

        column_cells = [self._column_cells(i) for i in range(num_cols)]

        while budget > 0:
            best_col = -1
            best_ratio = 0.0
            best_cost = 0
            best_benefit = 0.0

            for col_idx, cells in enumerate(column_cells):
                current = widths[col_idx]
                breakpoints_above = self._column_breakpoints(col_idx, above=current)
                if not breakpoints_above:
                    continue
                next_breakpoint = breakpoints_above[0]
                cost = next_breakpoint - current
                if cost > budget:
                    continue
                raw_benefit = sum(
                    1
                    for cell in cells
                    if cell.effective_width(next_breakpoint) > cell.effective_width(current)
                )
                if raw_benefit == 0:
                    continue
                benefit = raw_benefit * self._effective_priority(col_idx, current)
                ratio = benefit / cost if cost > 0 else 0.0
                if ratio > best_ratio or (ratio == best_ratio and benefit > best_benefit):
                    best_ratio = ratio
                    best_col = col_idx
                    best_cost = cost
                    best_benefit = benefit

            if best_col == -1:
                # No affordable breakpoint — spread remainder round-robin across
                # columns that still have abbreviated cells so they at least
                # have a chance to reach the next variant.
                abbreviated_cols = [
                    i for i in range(num_cols)
                    if any(cell.max_width > widths[i] for cell in column_cells[i])
                ]
                if abbreviated_cols:
                    for i in range(budget):
                        widths[abbreviated_cols[i % len(abbreviated_cols)]] += 1
                break

            widths[best_col] += best_cost
            budget -= best_cost

        return widths

    def _donor_capacity(self, col_idx: int, current_width: int, min_width: int) -> int:
        """How far can this column shrink without any cell dropping a variant?

        Shrinking from `current_width` is safe down to the greatest per-cell
        effective width (the highest variant currently displayed across cells).
        """
        cells = self._column_cells(col_idx)
        if not cells:
            return 0
        effective_now = max(cell.effective_width(current_width) for cell in cells)
        floor = max(effective_now, min_width)
        return max(0, current_width - floor)

    def _receiver_need(self, col_idx: int, current_width: int) -> int | None:
        """Width needed to advance at least one cell to its next variant.

        Returns None when every cell is already at its widest variant.
        """
        breakpoints = self._column_breakpoints(col_idx, above=current_width)
        if not breakpoints:
            return None
        return breakpoints[0] - current_width

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

    def _render_cell(self, cell: ResponsiveCell, width: int) -> Text:
        """Render a cell at the given width.

        Picks the widest variant that fits. If even the narrowest variant is
        wider than `width`, ellipsis-truncates it (the cell may prefer to be
        dropped instead — see `cell.refuses` — but column dropping is not yet
        implemented).
        """
        chosen = cell.render_at(width)
        if cell_len(chosen.plain) > width:
            text = chosen.copy()
            text.truncate(width, overflow="ellipsis")
            return text
        return chosen

    def _render_row(
        self,
        cells: Sequence[ResponsiveCell],
        widths: list[int],
        is_header: bool = False,
    ) -> Iterable[Segment]:
        """Render a single row of the table."""
        if self.box_style:
            border_char = self.box_style.head_left if is_header else self.box_style.mid_left
            yield Segment(border_char)

        pad_left = self.padding // 2
        pad_right = self.padding - pad_left

        for i, (cell, width) in enumerate(zip(cells, widths)):
            yield Segment(" " * pad_left)

            text = self._render_cell(cell, width)
            if is_header:
                text = text.copy()
                text.stylize("bold")
            content_width = cell_len(text.plain)

            for seg in text.render(Console()):
                yield seg

            padding_needed = width - content_width
            if padding_needed > 0:
                yield Segment(" " * padding_needed)

            yield Segment(" " * pad_right)

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
        box_widths = [w + self.padding for w in content_widths]

        if self.box_style:
            yield Segment(self.box_style.get_top(box_widths))
            yield Segment("\n")

        headers = [col.header for col in self._columns]
        yield from self._render_row(headers, content_widths, is_header=True)

        if self.box_style:
            yield Segment(self.box_style.get_row(box_widths, level="head"))
            yield Segment("\n")

        for row in self._rows:
            yield from self._render_row(row, content_widths)

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


def _render_status_segments(
    segments: Sequence[tuple],
    drop_classes: frozenset[str] = frozenset(),
) -> Text:
    """Render status segments into a Text, optionally dropping by narrow_class.

    Segments are 3-tuples (value, style, narrow_class). Segments whose
    narrow_class is in `drop_classes` are omitted. If nothing remains, returns
    the bare "clean" marker (caller decides whether that's appropriate).
    """
    kept = [s for s in segments if len(s) < 3 or s[2] not in drop_classes]
    if not kept:
        return Text("clean", style="green")
    text = Text()
    for idx, seg in enumerate(kept):
        value, style = seg[0], seg[1]
        if idx:
            text.append(" ")
        text.append(value, style=style)
    return text


def _status_cell(report: RepoReport) -> ResponsiveCell:
    """Status cell with a 3-step narrowing ladder.

    V0 full
    V1 drop (+N/-M)  — low-signal line-count detail
    V2 also drop submodule count
    """
    if not report.status_segments:
        return ResponsiveCell(variants=(Text("clean", style="green"),))

    v0 = _render_status_segments(report.status_segments)
    v1 = _render_status_segments(report.status_segments, frozenset({"plus_minus"}))
    v2 = _render_status_segments(
        report.status_segments, frozenset({"plus_minus", "extras"})
    )
    # Dedupe: if narrowing doesn't shorten anything, don't add a spurious variant.
    variants: list[Text] = [v0]
    for candidate in (v1, v2):
        if cell_len(candidate.plain) < cell_len(variants[-1].plain):
            variants.append(candidate)
    return ResponsiveCell(variants=tuple(variants))


def _mtime_cell(value: float | None) -> ResponsiveCell:
    """Timestamp cell: full → date → month-day → mmdd."""
    if value is None:
        return ResponsiveCell(variants=(Text("-"),))
    ts = datetime.fromtimestamp(value)
    variants = (
        Text(ts.strftime("%Y-%m-%d %H:%M")),
        Text(ts.strftime("%Y-%m-%d")),
        Text(ts.strftime("%m-%d")),
        Text(ts.strftime("%m%d")),
    )
    return ResponsiveCell(variants=variants)


def _branch_remote_cell(branch: str, remote: str) -> ResponsiveCell:
    """'master:origin/master' → 'master:origin' → 'master:…'."""
    colon = (":", "color(240)")
    v0 = Text.assemble(branch, colon, remote)
    variants: list[Text] = [v0]
    if "/" in remote:
        head = remote.split("/", 1)[0]
        variants.append(Text.assemble(branch, colon, head))
    variants.append(Text.assemble(branch, colon, "…"))
    # Keep only strictly-shortening steps.
    deduped: list[Text] = [variants[0]]
    for candidate in variants[1:]:
        if cell_len(candidate.plain) < cell_len(deduped[-1].plain):
            deduped.append(candidate)
    return ResponsiveCell(variants=tuple(deduped))


def _url_cell(url: str) -> ResponsiveCell:
    """'owner/repo' → 'owner/…' when a '/' exists."""
    v0 = Text(url)
    variants: list[Text] = [v0]
    if "/" in url:
        owner = url.split("/", 1)[0]
        short = Text(f"{owner}/…")
        if cell_len(short.plain) < cell_len(v0.plain):
            variants.append(short)
    return ResponsiveCell(variants=tuple(variants))


def _header_cell(*labels: str) -> ResponsiveCell:
    """Shortcut for a header with simple plain-Text variants."""
    return ResponsiveCell(variants=tuple(Text(lbl) for lbl in labels))


def _branch_remote_header() -> ResponsiveCell:
    colon = (":", "color(240)")
    return ResponsiveCell(variants=(
        Text.assemble("Branch", colon, "Remote"),
        Text.assemble("Br", colon, "Rm"),
    ))


DEFAULT_COLUMNS = ["dir", "status", "branch_remote", "url", "mtime"]

# (header_factory, priority). header_factory returns a ResponsiveCell each
# call so headers can't accidentally share mutable state.
_COLUMN_DEFS: dict[str, tuple[Callable[[], ResponsiveCell], int]] = {
    "dir": (lambda: _header_cell("Dir"), 50),
    "status": (lambda: _header_cell("Status"), 50),
    "branch_remote": (_branch_remote_header, 50),
    "branch": (lambda: _header_cell("Branch", "Br"), 50),
    "remote": (lambda: _header_cell("Remote", "Rm"), 30),
    "url": (lambda: _header_cell("URL"), 30),
    "mtime": (lambda: _header_cell("Modified", "Mtime"), 30),
    "ident": (lambda: _header_cell("Ident"), 1),
}


def _row_value(col: str, report: RepoReport) -> ResponsiveCell:
    if col == "dir":
        return _as_responsive(report.display_path)
    if col == "status":
        return _status_cell(report)
    if col == "branch_remote":
        return _branch_remote_cell(report.branch, report.remote)
    if col == "branch":
        return _as_responsive(report.branch)
    if col == "remote":
        return _as_responsive(report.remote)
    if col == "url":
        return _url_cell(report.remote_url)
    if col == "mtime":
        return _mtime_cell(report.latest_mtime)
    if col == "ident":
        return _as_responsive(report.ident or "-")
    raise ValueError(f"Unknown column: {col!r}")


def parse_columns(spec: str) -> list[str]:
    """Parse a column spec string into an ordered list of column identifiers.

    Tokens are comma-separated and processed left-to-right:
      ``-``      clear all columns
      ``-col``   remove *col*
      ``col``    append *col* (moves to end if already present)

    Last mention of a column wins.
    """
    result = list(DEFAULT_COLUMNS)
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token == "-":
            result.clear()
        elif token.startswith("-"):
            name = token[1:]
            if name not in _COLUMN_DEFS:
                raise ValueError(f"Unknown column: {name!r}")
            if name in result:
                result.remove(name)
        else:
            if token not in _COLUMN_DEFS:
                raise ValueError(f"Unknown column: {token!r}")
            if token in result:
                result.remove(token)
            result.append(token)
    return result


def render_table(
    console: Console,
    reports: Sequence[RepoReport],
    *,
    minimize_chars: bool = False,
    columns: list[str] | None = None,
) -> None:
    active_columns = columns if columns is not None else DEFAULT_COLUMNS

    show_exceptional_key = any(
        any(seg[0] == "!" for seg in report.status_segments) for report in reports
    )

    # Allow priority override via environment variable
    env_priorities = os.environ.get("GITOVERIT_COLUMN_PRIORITIES")
    priority_overrides: dict[str, int] = {}
    if env_priorities:
        try:
            vals = [int(p.strip()) for p in env_priorities.split(",")]
            if len(vals) == len(DEFAULT_COLUMNS):
                priority_overrides = dict(zip(DEFAULT_COLUMNS, vals))
        except ValueError:
            pass

    table = AutoTable(width="fill", minimize_chars=minimize_chars)
    for col in active_columns:
        header_factory, default_priority = _COLUMN_DEFS[col]
        priority = priority_overrides.get(col, default_priority)
        table.add_column(header_factory(), priority=priority)

    for report in reports:
        table.add_row(*[_row_value(col, report) for col in active_columns])

    console.print(table)

    if reports and "status" in active_columns:
        console.print(_status_key_main())
        if show_exceptional_key:
            console.print(_status_key_exceptional())


__all__ = [
    "AutoColumn",
    "AutoTable",
    "DEFAULT_COLUMNS",
    "ResponsiveCell",
    "parse_columns",
    "render_table",
]
