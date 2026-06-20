"""Compact braille sparkline (T3 R2) — a dependency-free "heartbeat" renderer.

Braille Unicode (U+2800..U+28FF) packs a 2x4 dot grid into one character. We treat each
cell as **two columns** (left/right) of **four stackable rows**, so a single braille
character draws two sample bars side by side. A strip of N samples therefore needs only
ceil(N/2) characters — a genuinely compact pulse that spikes when active, flat when idle.

No heavy dependency: this is ~40 lines of pure stdlib mapping ints -> a unicode string.
The caller styles/labels it; this module only turns a list of values into braille glyphs.

Dot numbering inside a braille cell (the standard layout):

    1 4        bit 0  bit 3      (top row)
    2 5        bit 1  bit 4
    3 6        bit 2  bit 5
    7 8        bit 6  bit 7      (bottom row)

We fill from the bottom up, so taller values = more dots rising from the baseline.
"""

from __future__ import annotations

_BRAILLE_BASE = 0x2800

# Left column bits top->bottom (rows 0..3); right column bits top->bottom.
# Filling bottom-up means height 1 lights only the lowest row, height 4 all four.
_LEFT_BITS = (0, 1, 2, 6)  # rows top..bottom for the left column
_RIGHT_BITS = (3, 4, 5, 7)  # rows top..bottom for the right column


def _column_mask(bits: tuple[int, ...], height: int) -> int:
    """Light `height` dots (0..4) from the bottom of one column."""
    height = max(0, min(4, height))
    mask = 0
    for i in range(height):
        mask |= 1 << bits[len(bits) - 1 - i]  # bottom row first
    return mask


def _heights(values: list[float], peak: float, levels: int = 4) -> list[int]:
    """Scale each value to an integer dot-height in 0..levels.

    A flat/empty series (peak <= 0) maps every sample to a single baseline dot, so the
    strip reads as a quiet flat line rather than vanishing. Any positive value gets at
    least one dot so a real (tiny) spike is never invisible.
    """
    if not values:
        return []
    if peak <= 0:
        return [1] * len(values)  # baseline flat line
    out: list[int] = []
    for v in values:
        if v <= 0:
            out.append(1)  # idle baseline dot
            continue
        h = int(round((v / peak) * levels))
        out.append(max(1, min(levels, h)))
    return out


def sparkline(values: list[float], peak: float | None = None) -> str:
    """Render `values` as a braille strip. `peak` defaults to max(values).

    Returns a string of braille characters (two samples per character). An empty list
    yields an empty string; the caller decides what to show for "no activity".
    """
    if not values:
        return ""
    if peak is None:
        peak = max(values) if values else 0.0
    heights = _heights(values, peak)

    chars: list[str] = []
    for i in range(0, len(heights), 2):
        left = heights[i]
        right = heights[i + 1] if i + 1 < len(heights) else 0
        code = _BRAILLE_BASE | _column_mask(_LEFT_BITS, left) | _column_mask(_RIGHT_BITS, right)
        chars.append(chr(code))
    return "".join(chars)


# ── Multi-row chart (T4) ──────────────────────────────────────────────────────
# A braille cell already stacks 4 dots per column. To draw a *taller* chart we stack
# several braille character-rows: `rows` rows give `rows * 4` vertical dot-levels. A
# value is scaled to that many levels, then split across the rows bottom-up — full
# lower rows get all 4 dots, the partial row gets the remainder, higher rows stay empty.
# Each row is rendered with the same two-samples-per-cell packing as `sparkline`, so a
# chart of N samples is `ceil(N/2)` characters wide and exactly `rows` lines tall.

_DOTS_PER_ROW = 4  # braille dots stacked per column within one character row


def _row_levels(total: int, rows: int) -> list[int]:
    """Split a 0..rows*4 dot-height into per-row dot counts (0..4), bottom row first.

    Returns `rows` ints. index 0 is the *bottom* character row, index rows-1 the top.
    """
    total = max(0, min(rows * _DOTS_PER_ROW, total))
    out: list[int] = []
    for _ in range(rows):
        out.append(max(0, min(_DOTS_PER_ROW, total)))
        total -= _DOTS_PER_ROW
    return out


def _scaled_levels(values: list[float], peak: float, rows: int) -> list[int]:
    """Scale each value to a 0..rows*4 integer dot-height (the chart's vertical span).

    The peak value maps to the full height (`rows*4`) so the tallest sample reaches the
    top row (H2). A flat/empty series (peak <= 0) maps everything to a single baseline
    dot; any positive value gets at least one dot so a tiny spike never vanishes.
    """
    levels_max = rows * _DOTS_PER_ROW
    if not values:
        return []
    if peak <= 0:
        return [1] * len(values)
    out: list[int] = []
    for v in values:
        if v <= 0:
            out.append(1)  # idle baseline dot
            continue
        h = int(round((v / peak) * levels_max))
        out.append(max(1, min(levels_max, h)))
    return out


def chart_rows(values: list[float], peak: float | None, rows: int) -> list[str]:
    """Render `values` as a `rows`-line braille column chart, top row first.

    The tallest sample (== `peak`) reaches the top row. Returns exactly `rows` strings,
    each `ceil(len(values)/2)` braille characters wide (two samples per character, as in
    `sparkline`). An empty `values` yields `rows` empty strings. `peak` defaults to the
    max of `values`; a non-positive peak draws a flat baseline (handles all-equal / single
    bucket without dividing by zero).
    """
    rows = max(1, int(rows))
    if not values:
        return [""] * rows
    if peak is None:
        peak = max(values) if values else 0.0
    heights = _scaled_levels(values, peak, rows)

    # Per-sample per-row dot counts, bottom row (index 0) .. top row (index rows-1).
    split = [_row_levels(h, rows) for h in heights]

    out_bottom_up: list[str] = []
    for row in range(rows):
        chars: list[str] = []
        for i in range(0, len(split), 2):
            left = split[i][row]
            right = split[i + 1][row] if i + 1 < len(split) else 0
            code = (
                _BRAILLE_BASE
                | _column_mask(_LEFT_BITS, left)
                | _column_mask(_RIGHT_BITS, right)
            )
            chars.append(chr(code))
        out_bottom_up.append("".join(chars))
    out_bottom_up.reverse()  # top row first
    return out_bottom_up
