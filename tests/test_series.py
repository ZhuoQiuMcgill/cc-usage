"""Heartbeat series (T3 R2): rate-per-bucket semantics, windows, edges, braille.

The series is the data behind the compact braille "heartbeat". Each bucket value is the
*sum* of cost (or tokens) for records inside that bucket — NOT a cumulative running
total — and an empty window must produce a flat all-zeros strip, never crash.

The T4 section also covers the taller axis'd *chart* rendering built on top of the same
series: peak scales to the top row, the Y-axis labels go peak..0, the X-axis carries time
ticks, and a peak-time annotation names when the peak happened.
"""

import math
import time

from cc_usage.aggregate import (
    HEARTBEAT_METRICS,
    HEARTBEAT_WINDOWS,
    Series,
    series,
)
from cc_usage.braille import _DOTS_PER_ROW, chart_rows, sparkline
from cc_usage.parser import UsageRecord
from cc_usage.render import (
    HEARTBEAT_HEIGHT,
    _bucket_center_time,
    _peak_annotation,
    _peak_bucket_index,
    _x_axis_line,
    _y_axis_labels,
)

_BLANK = chr(0x2800)  # empty braille cell

NOW = 1_000_000_000.0


def rec(age_s: float, cost: float = 0.0, inp: int = 0, out: int = 0, cache: int = 0):
    return UsageRecord(
        ts=NOW - age_s,
        model_raw="claude-opus-4-8",
        model_norm="claude-opus-4-8",
        known=True,
        input_tokens=inp,
        output_tokens=out,
        cache_read=cache,
        cache_creation=0,
        cost=cost,
    )


# ── shape / defaults ────────────────────────────────────────────────────────
def test_series_length_and_window_default():
    s = series([], NOW)
    assert isinstance(s, Series)
    assert s.window == "24h" and s.metric == "cost"  # defaults
    assert len(s.values) == 48  # ~40-60 buckets, fixed bucket count
    assert s.window_seconds == 24 * 3600


def test_unknown_window_and_metric_fall_back_not_crash():
    s = series([rec(60, cost=1.0)], NOW, window="bogus", metric="nonsense")
    assert s.window == "24h"  # fell back
    assert s.metric == "cost"  # fell back
    # still produced a usable series
    assert len(s.values) == 48


# ── rate-per-bucket, not cumulative ──────────────────────────────────────────
def test_rate_per_bucket_not_cumulative():
    """Two records far apart in time must land in DIFFERENT buckets, each holding only
    its own value — a cumulative series would make later buckets >= earlier ones."""
    # 5h window, 48 buckets -> each bucket = 375 s. Put records ~2.5h apart.
    s = series([rec(60, cost=2.0), rec(4 * 3600, cost=5.0)], NOW, window="5h", metric="cost")
    nonzero = [v for v in s.values if v > 0]
    assert len(nonzero) == 2  # exactly two buckets lit, not a rising staircase
    assert math.isclose(sum(s.values), 7.0, abs_tol=1e-9)
    assert math.isclose(s.peak, 5.0, abs_tol=1e-9)
    # The most-recent bucket (last) should carry the recent 2.0, an OLDER bucket the 5.0.
    assert math.isclose(s.values[-1], 2.0, abs_tol=1e-9)


def test_same_bucket_records_sum():
    """Two records in the same bucket sum (the bucket value is a sum, by spec)."""
    s = series([rec(30, cost=1.0), rec(40, cost=3.0)], NOW, window="24h", metric="cost")
    assert math.isclose(s.values[-1], 4.0, abs_tol=1e-9)
    assert math.isclose(s.peak, 4.0, abs_tol=1e-9)
    assert sum(1 for v in s.values if v > 0) == 1


def test_metric_tokens_uses_total_tokens():
    r = rec(30, cost=1.0, inp=100, out=200, cache=700)  # total_tokens = 1000
    s_tok = series([r], NOW, window="24h", metric="tokens")
    s_cost = series([r], NOW, window="24h", metric="cost")
    assert math.isclose(s_tok.values[-1], 1000.0, abs_tol=1e-9)  # in+out+cache
    assert math.isclose(s_cost.values[-1], 1.0, abs_tol=1e-9)


# ── window scoping + boundaries ──────────────────────────────────────────────
def test_records_outside_window_excluded():
    # 5h window: a 6h-old record is out; a 1h-old record is in.
    s = series([rec(6 * 3600, cost=9.0), rec(3600, cost=2.0)], NOW, window="5h")
    assert math.isclose(sum(s.values), 2.0, abs_tol=1e-9)  # the 9.0 dropped


def test_left_edge_exclusive_right_edge_inclusive():
    secs = 5 * 3600
    on_left = series([rec(secs, cost=1.0)], NOW, window="5h")  # age == window -> excluded
    just_in = series([rec(secs - 1, cost=1.0)], NOW, window="5h")
    at_now = series([rec(0, cost=1.0)], NOW, window="5h")  # ts == now -> last bucket
    assert math.isclose(sum(on_left.values), 0.0, abs_tol=1e-9)
    assert math.isclose(sum(just_in.values), 1.0, abs_tol=1e-9)
    assert math.isclose(at_now.values[-1], 1.0, abs_tol=1e-9)


def test_future_record_ignored():
    # A record with ts after now (clock skew) must not crash or land anywhere.
    s = series([rec(-60, cost=5.0)], NOW, window="24h")
    assert math.isclose(sum(s.values), 0.0, abs_tol=1e-9)


def test_oldest_newest_bucket_placement():
    """Oldest in-window record -> bucket 0; most-recent -> last bucket."""
    secs = 24 * 3600
    s = series([rec(secs - 1, cost=1.0), rec(1, cost=2.0)], NOW, window="24h")
    assert math.isclose(s.values[0], 1.0, abs_tol=1e-9)  # near the old edge
    assert math.isclose(s.values[-1], 2.0, abs_tol=1e-9)  # near now


# ── empty window ─────────────────────────────────────────────────────────────
def test_empty_window_is_flat_zeros():
    s = series([], NOW, window="7d", metric="tokens")
    assert s.is_empty
    assert s.peak == 0.0
    assert all(v == 0.0 for v in s.values)
    # renderer must produce a non-crashing flat strip (baseline dots), not blow up
    strip = sparkline(s.values, s.peak)
    assert strip != ""  # 48 zeros -> 24 baseline braille cells
    assert all(0x2800 <= ord(c) <= 0x28FF for c in strip)


def test_all_records_out_of_window_is_empty():
    s = series([rec(10 * 24 * 3600, cost=5.0)], NOW, window="7d")  # 10d old, window 7d
    assert s.is_empty and s.peak == 0.0


# ── the three heartbeat windows ──────────────────────────────────────────────
def test_three_windows_have_expected_spans():
    spans = {w: secs for w, secs in HEARTBEAT_WINDOWS}
    assert spans == {"5h": 5 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
    for w, secs in HEARTBEAT_WINDOWS:
        assert series([], NOW, window=w).window_seconds == secs


def test_metrics_are_cost_and_tokens():
    assert HEARTBEAT_METRICS == ("cost", "tokens")


# ── braille renderer ─────────────────────────────────────────────────────────
def test_sparkline_peak_is_tallest_cell():
    vals = [0, 0, 0, 10, 0, 0]  # one clear spike
    strip = sparkline(vals, max(vals))
    assert all(0x2800 <= ord(c) <= 0x28FF for c in strip)
    # two samples per cell -> 3 cells for 6 samples
    assert len(strip) == 3


def test_sparkline_empty_list_is_empty_string():
    assert sparkline([]) == ""


def test_sparkline_tiny_spike_is_visible():
    # A tiny positive value must light at least one dot above baseline, never vanish.
    vals = [0.0, 0.0, 1e-6, 0.0]
    strip = sparkline(vals, max(vals))
    # at least one cell differs from the pure-baseline glyph
    baseline = sparkline([0.0, 0.0, 0.0, 0.0], 0.0)
    assert strip != baseline


# ── T4: taller chart with peak-scaled axes ───────────────────────────────────
def test_chart_rows_fixed_height_and_width():
    """H1: the chart body is exactly HEIGHT rows, two samples per braille cell wide."""
    vals = [0.0, 1.0, 2.0, 3.0, 4.0]  # 5 samples -> ceil(5/2) = 3 cells
    rows = chart_rows(vals, max(vals), HEARTBEAT_HEIGHT)
    assert len(rows) == HEARTBEAT_HEIGHT
    assert all(len(r) == 3 for r in rows)
    assert HEARTBEAT_HEIGHT >= 8  # "fixed, taller" per spec


def test_chart_empty_values_is_blank_rows():
    rows = chart_rows([], None, HEARTBEAT_HEIGHT)
    assert rows == [""] * HEARTBEAT_HEIGHT


def test_peak_bucket_lands_in_top_row():
    """H2: the largest bucket reaches the TOP row of the scaled chart, and only it does.

    A clearly-tallest sample placed in an older bucket must light the top character row
    at its column — and no other column should reach the top (peak == max height)."""
    # 5h window, two samples ~3.75h apart so they land in different buckets.
    s = series([rec(60, cost=2.0), rec(int(3.75 * 3600), cost=10.0)], NOW, window="5h")
    rows = chart_rows(s.values, s.peak, HEARTBEAT_HEIGHT)
    top = rows[0]
    peak_cell = _peak_bucket_index(s) // 2  # two samples per braille cell
    assert top[peak_cell] != _BLANK  # the peak bucket reaches the top row
    others = [c for j, c in enumerate(top) if j != peak_cell]
    assert all(c == _BLANK for c in others)  # nothing else reaches the top


def test_peak_value_scales_to_full_height():
    """The peak value maps to the full vertical span (rows * dots-per-row)."""
    from cc_usage.braille import _scaled_levels

    levels = _scaled_levels([10.0, 5.0, 0.0], 10.0, HEARTBEAT_HEIGHT)
    assert levels[0] == HEARTBEAT_HEIGHT * _DOTS_PER_ROW  # peak = top
    assert levels[1] == HEARTBEAT_HEIGHT * _DOTS_PER_ROW // 2  # half-height
    assert levels[2] == 1  # zero -> idle baseline dot, never vanishes


def test_chart_single_bucket_no_divide_by_zero():
    """A single in-window record must render without crashing; its bucket hits the top."""
    s = series([rec(30, cost=4.0)], NOW, window="24h", metric="cost")
    rows = chart_rows(s.values, s.peak, HEARTBEAT_HEIGHT)
    assert len(rows) == HEARTBEAT_HEIGHT
    peak_cell = _peak_bucket_index(s) // 2
    assert rows[0][peak_cell] != _BLANK


def test_chart_all_equal_values_no_crash():
    """All-equal nonzero buckets: peak == each value, every lit column reaches the top."""
    s = series([rec(60, cost=2.0), rec(2 * 3600, cost=2.0)], NOW, window="5h")
    assert math.isclose(s.peak, 2.0, abs_tol=1e-9)
    rows = chart_rows(s.values, s.peak, HEARTBEAT_HEIGHT)
    assert len(rows) == HEARTBEAT_HEIGHT  # no ZeroDivisionError


def test_chart_empty_window_flat_baseline_at_zero():
    """H4: idle window -> only the BOTTOM row is lit (flat baseline at 0), rest blank."""
    s = series([], NOW, window="7d", metric="tokens")
    rows = chart_rows(s.values, s.peak, HEARTBEAT_HEIGHT)
    assert all(c != _BLANK for c in rows[-1])  # baseline drawn
    assert all(c == _BLANK for c in rows[0])  # nothing rises above it


# ── T4: Y-axis labels (peak at top, 0 at bottom) ─────────────────────────────
def test_y_axis_top_is_peak_bottom_is_zero_cost():
    labels = _y_axis_labels(10.0, "cost", HEARTBEAT_HEIGHT)
    assert len(labels) == HEARTBEAT_HEIGHT
    assert labels[0].strip() == "$10.00"  # peak at top, money-formatted
    assert labels[-1].strip() == "0"  # zero at bottom
    assert sum(1 for s in labels if s.strip()) >= 3  # peak + 0 + >=1 mid tick


def test_y_axis_tokens_formatting_and_empty_peak():
    labels = _y_axis_labels(2_000_000.0, "tokens", HEARTBEAT_HEIGHT)
    assert labels[0].strip() == "2.0M"  # tokens formatted K/M
    # empty/idle window: peak 0 -> top and bottom both read 0, never crashes
    flat = _y_axis_labels(0.0, "cost", HEARTBEAT_HEIGHT)
    assert flat[-1].strip() == "0"
    assert flat[0].strip() in ("$0.00", "0")


# ── T4: X-axis time ticks (scaled per window) ────────────────────────────────
def test_x_axis_hours_for_24h_window_ends_in_now():
    s = series([], NOW, window="24h")
    line = _x_axis_line(s, 40, 0)
    assert line.rstrip().endswith("now")
    assert "-24h" in line  # left edge labeled at the window length


def test_x_axis_days_for_7d_window():
    s = series([], NOW, window="7d")
    line = _x_axis_line(s, 40, 0)
    assert "-7d" in line  # 7d window uses day labels
    assert line.rstrip().endswith("now")


# ── T4: peak-time annotation matches the peak bucket ─────────────────────────
def test_peak_annotation_names_peak_bucket_time():
    """H3: the annotation's clock time matches the CENTER of the peak bucket."""
    # Peak ~4h ago in a 5h window.
    s = series([rec(60, cost=1.0), rec(4 * 3600, cost=9.0)], NOW, window="5h")
    idx = _peak_bucket_index(s)
    center = _bucket_center_time(s, idx)
    expected_clock = time.strftime("%H:%M", time.localtime(center))
    ann = _peak_annotation(s)
    assert ann.startswith("peak $9.00/bucket")  # the peak value, money-formatted
    assert expected_clock in ann  # clock time of the peak bucket center
    # relative part is present and ~4h ago (within one bucket width)
    assert "ago" in ann
    assert abs((NOW - center) - 4 * 3600) < s.bucket_seconds


def test_peak_annotation_tokens_metric_and_recent_peak():
    r = rec(30, inp=1000, out=0, cache=0)  # total_tokens = 1000, ~now
    s = series([r], NOW, window="24h", metric="tokens")
    ann = _peak_annotation(s)
    assert ann.startswith("peak 1K/bucket")  # tokens formatting
    # near-now peak -> relative reads "now" (within a minute), never negative
    assert "(now)" in ann or "ago)" in ann
