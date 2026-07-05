"""`limits_block` rate-limit rendering, incl. the stale-after-reset fix (T10).

Unit tests of the pure renderer, mirroring the render-helper style in test_series.py /
test_range.py: build synthetic Buckets + a minimal RenderState, render to plain text and
assert on it. No I/O, no real capture file — everything here is fabricated.

The fix under test: the 5h/7d capture only refreshes on a Claude Code turn, so once a
bucket's reset moment has passed the last-captured percentage is stale. At render time a
bucket with now >= resets_at must show 0% + an empty bar + a "reset … ago" note, never a
fabricated next countdown, and the row must flip live the first tick after the boundary.
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from cc_usage.config import Config
from cc_usage.format import human_duration
from cc_usage.ratelimits import Bucket
from cc_usage.render import RenderState, limits_block
from cc_usage.themes import get_theme

NOW = 1_000_000_000.0
THEME = get_theme("dark")


def _plain(renderable) -> str:
    """Render a Rich renderable to plain (uncolored) text for assertions."""
    buf = io.StringIO()
    Console(file=buf, width=80, no_color=True).print(renderable)
    return buf.getvalue()


def _state(buckets: list[Bucket], now: float = NOW) -> RenderState:
    # limits_block only reads .buckets/.now/.rl_present; windows is irrelevant here.
    return RenderState(windows={}, buckets=buckets, now=now, config=Config(), interval=5)


def _bucket(resets_at: float, key: str = "five_hour", label: str = "5-HOUR", pct: float = 85.0):
    return Bucket(key=key, label=label, used_percentage=pct, resets_at=resets_at)


# ── 1. expired window ─────────────────────────────────────────────────────────
def test_expired_bucket_zeroes_and_says_ago_not_resets_in():
    """now past resets_at -> 0%, empty bar, a 'reset … ago' note, no next countdown."""
    out = _plain(limits_block(_state([_bucket(NOW - 10_800)]), THEME))  # reset 3h ago
    assert "0%" in out
    assert "85%" not in out  # the stale captured percentage is NOT echoed
    assert "▓" not in out and "░" in out  # bar drained to empty
    assert "ago" in out
    assert "resets in" not in out  # must not fabricate a next-window countdown
    assert "awaiting next turn" in out  # signals fresh data needs a Claude Code turn


# ── 2. boundary counts as expired ─────────────────────────────────────────────
def test_boundary_now_equals_resets_at_is_expired():
    """now == resets_at is expired (>=): still 0% + 'ago', never 'resets in'."""
    out = _plain(limits_block(_state([_bucket(NOW)]), THEME))
    assert "0%" in out
    assert "ago" in out
    assert "resets in" not in out


# ── 3. buckets judged independently ───────────────────────────────────────────
def test_mixed_expired_five_hour_and_active_weekly():
    """One expired 5h + one active weekly render as one expired row + one normal row."""
    buckets = [
        _bucket(NOW - 60, key="five_hour", label="5-HOUR"),  # expired 1m ago
        _bucket(NOW + 86_400, key="seven_day", label="WEEKLY", pct=40.0),  # active, 1d left
    ]
    out = _plain(limits_block(_state(buckets), THEME))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2
    five, weekly = lines[0], lines[1]  # deterministic order: five_hour before seven_day
    # expired 5h row
    assert five.startswith("5-HOUR") and "0%" in five and "ago" in five and "resets in" not in five
    # active weekly row untouched
    assert weekly.startswith("WEEKLY") and "40%" in weekly and "resets in" in weekly and "ago" not in weekly


# ── 4. active rendering is byte-identical to before the fix (regression guard) ──
def test_active_bucket_renders_byte_identical():
    """An active window must render exactly as it did pre-fix: bar, NN%, 'resets in <dur>'."""
    b = _bucket(NOW + 7_860, pct=85.0)  # resets in 2h11m
    out = _plain(limits_block(_state([b]), THEME))
    assert out == "5-HOUR  ▓▓▓▓▓▓▓▓▓▓▓▓░░  85%  resets in 2h11m\n"


# ── 5. no negative duration may ever render ───────────────────────────────────
def test_human_duration_never_negative():
    """The contract the call site relies on: <= 0 clamps to 'now', never a '-<n>' string."""
    assert human_duration(-1) == "now"
    assert human_duration(-10_000) == "now"
    assert human_duration(0) == "now"
    for s in (-1, -0.5, -100_000, 0):
        assert not human_duration(s).startswith("-")


def test_expired_render_emits_no_negative_duration():
    """Even for a long-expired bucket the 'ago' duration is >= 0 (now - resets_at), so the
    rendered note can never contain a '-<n>' countdown."""
    out = _plain(limits_block(_state([_bucket(NOW - 500_000)]), THEME))  # long expired
    # A negative duration would read like '-5d18h'; a minus glued to a digit is the tell.
    # (The '5-HOUR' label's hyphen is not followed by a digit, so it doesn't false-positive.)
    assert re.search(r"-\d", out) is None


# ── 6. live crossing flips the row on the first tick past resets_at ────────────
def test_live_crossing_flips_row_at_the_boundary():
    """Same bucket, no new capture: now = resets_at - 1 is active, now = resets_at + 1 is
    expired. Proves the fix is evaluated at render time (falls out of using state.now)."""
    b = _bucket(NOW, pct=85.0)  # resets exactly at NOW
    before = _plain(limits_block(_state([b], now=NOW - 1), THEME))  # 1s before reset
    after = _plain(limits_block(_state([b], now=NOW + 1), THEME))  # 1s after reset

    assert "85%" in before and "resets in" in before and "ago" not in before
    assert "0%" in after and "ago" in after and "resets in" not in after
    assert before != after  # the row visibly flipped


# ── empty / n-a states unchanged (guard the existing behaviour) ────────────────
def test_no_buckets_no_capture_shows_populate_hint():
    st = _state([])
    st.rl_present = False  # no capture file at all
    out = _plain(limits_block(st, THEME))
    assert "run a Claude Code turn to populate" in out


def test_rl_present_but_no_buckets_shows_capture_hint():
    st = _state([])
    st.rl_present = True  # capture exists but held no usable buckets
    out = _plain(limits_block(st, THEME))
    assert "capture present but no usable buckets yet" in out
