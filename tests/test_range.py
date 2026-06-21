"""Date-range aggregation (T7): inclusive bounds, hand-summed totals, LOCAL-calendar
per-day bucketing incl. zero days, empty range, and a local-midnight boundary record.

A date range is a human CALENDAR concept, so `aggregate_range` works in LOCAL time:
day buckets come from `datetime.fromtimestamp(r.ts).date()` and the picked dates become
epoch bounds via `day_start_ts`/`day_end_ts`. The TZ is therefore PINNED here (TZ env +
time.tzset()) and records are built at deterministic timestamps — no wall-clock `now` —
so the day boundaries are reproducible on any machine/CI.
"""

import datetime
import os
import time

import pytest

from cc_usage.aggregate import (
    DayAgg,
    RangeAgg,
    aggregate_range,
    day_end_ts,
    day_start_ts,
)
from cc_usage.parser import UsageRecord

# Pin to a fixed offset zone (no DST in these test dates) for deterministic local days.
_TZ = "America/New_York"  # UTC-5 in winter, UTC-4 in summer; our dates are in June (-4).


@pytest.fixture(autouse=True)
def _pin_tz():
    """Pin the process timezone for the whole module so local-day math is deterministic."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = _TZ
    time.tzset()
    yield
    if prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prev
    time.tzset()


def _ts(y, mo, d, h=12, mi=0, s=0, us=0) -> float:
    """A LOCAL wall-clock instant -> epoch seconds (respects the pinned TZ)."""
    return datetime.datetime(y, mo, d, h, mi, s, us).timestamp()


def rec(
    ts: float,
    inp: int = 0,
    out: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    cost: float = 0.0,
    model: str = "claude-opus-4-8",
    known: bool = True,
):
    return UsageRecord(
        ts=ts,
        model_raw=model,
        model_norm=model,
        known=known,
        input_tokens=inp,
        output_tokens=out,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cost=cost,
    )


# ── day-boundary helpers ────────────────────────────────────────────────────────
def test_day_bounds_span_a_full_local_day():
    d = datetime.date(2026, 6, 13)
    start = day_start_ts(d)
    end = day_end_ts(d)
    # start is local midnight, end is the last microsecond before the next midnight.
    assert datetime.datetime.fromtimestamp(start) == datetime.datetime(2026, 6, 13, 0, 0, 0)
    assert datetime.datetime.fromtimestamp(end).date() == d
    assert end < day_start_ts(datetime.date(2026, 6, 14))
    # Just under 24h apart (86400s minus 1µs).
    assert abs((end - start) - (86400 - 1e-6)) < 1e-3


# ── inclusivity at both ends ────────────────────────────────────────────────────
def test_inclusive_at_both_bounds_and_exclusive_just_outside():
    start = _ts(2026, 6, 13, 0, 0, 0)
    end = _ts(2026, 6, 20, 23, 59, 59, 999999)
    on_start = rec(start, inp=11)
    on_end = rec(end, inp=22)
    before = rec(start - 1e-6, inp=999)  # a hair below start -> excluded
    after = rec(end + 1e-6, inp=888)  # a hair above end -> excluded
    rng = aggregate_range([before, on_start, on_end, after], start, end)
    # Both edge records included; both just-outside records excluded.
    assert rng.record_count == 2
    assert rng.input_tokens == 33


# ── per-model + grand totals vs a hand-summed fixture ───────────────────────────
def test_per_model_and_grand_totals_hand_summed():
    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 20))
    records = [
        # Opus: cost already baked into the record (cache×0.10 / 5m×1.25 / 1h×2.00).
        rec(_ts(2026, 6, 13, 9), inp=100, out=20, cache_read=300, cost=2.50, model="claude-opus-4-8"),
        rec(_ts(2026, 6, 15, 14), inp=50, out=10, cache_creation=200, cost=1.25, model="claude-opus-4-8"),
        # Sonnet on a different day.
        rec(_ts(2026, 6, 18, 8), inp=400, out=80, cache_read=0, cost=4.00, model="claude-sonnet-4-6"),
        # Unknown model: counts tokens, contributes $0, flagged.
        rec(_ts(2026, 6, 19, 22), inp=70, out=5, cost=0.0, model="mystery-model", known=False),
    ]
    rng = aggregate_range(records, start, end)

    # Grand totals (hand-summed).
    assert rng.input_tokens == 100 + 50 + 400 + 70  # 620
    assert rng.output_tokens == 20 + 10 + 80 + 5  # 115
    assert rng.cache_tokens == 300 + 200 + 0 + 0  # 500
    assert rng.total_tokens == 620 + 115 + 500  # 1235
    assert rng.cost == pytest.approx(2.50 + 1.25 + 4.00 + 0.0)  # 7.75
    assert rng.record_count == 4

    # Per-model breakdown.
    by = {m.model: m for m in rng.models_sorted()}
    assert by["claude-opus-4-8"].input_tokens == 150
    assert by["claude-opus-4-8"].output_tokens == 30
    assert by["claude-opus-4-8"].cache_tokens == 500
    assert by["claude-opus-4-8"].cost == pytest.approx(3.75)
    assert by["claude-sonnet-4-6"].cost == pytest.approx(4.00)
    assert by["mystery-model"].known is False
    assert by["mystery-model"].cost == pytest.approx(0.0)

    # Sort order: cost desc -> sonnet(4.00) > opus(3.75) > unknown(0.0).
    order = [m.model for m in rng.models_sorted()]
    assert order == ["claude-sonnet-4-6", "claude-opus-4-8", "mystery-model"]


# ── per-day LOCAL bucketing incl. zero days ─────────────────────────────────────
def test_per_day_local_bucketing_includes_zero_days():
    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 20))
    records = [
        rec(_ts(2026, 6, 13, 1), inp=10, cost=1.0),  # first day
        rec(_ts(2026, 6, 13, 23), inp=5, cost=0.5),  # same day, later
        rec(_ts(2026, 6, 16, 12), inp=100, cost=9.0),  # mid-range day
        rec(_ts(2026, 6, 20, 12), inp=1, cost=0.1),  # last day
    ]
    rng = aggregate_range(records, start, end)

    # 8 calendar days, June 13..20 inclusive, chronological, every day present.
    assert rng.n_days == 8
    assert [d.date for d in rng.days] == [
        datetime.date(2026, 6, day) for day in range(13, 21)
    ]
    by_date = {d.date: d for d in rng.days}
    # Day 13 got both records summed.
    assert by_date[datetime.date(2026, 6, 13)].input_tokens == 15
    assert by_date[datetime.date(2026, 6, 13)].cost == pytest.approx(1.5)
    assert by_date[datetime.date(2026, 6, 16)].input_tokens == 100
    assert by_date[datetime.date(2026, 6, 20)].input_tokens == 1
    # Zero days really are zero.
    assert by_date[datetime.date(2026, 6, 14)].total_tokens == 0
    assert by_date[datetime.date(2026, 6, 15)].cost == 0.0
    # active_days = 3 (13, 16, 20); n_days = 8.
    assert rng.active_days == 3
    assert rng.n_days == 8


# ── empty range ─────────────────────────────────────────────────────────────────
def test_empty_range_zeros_but_days_still_span():
    start = day_start_ts(datetime.date(2026, 6, 1))
    end = day_end_ts(datetime.date(2026, 6, 3))
    rng = aggregate_range([], start, end)
    assert rng.record_count == 0
    assert rng.input_tokens == 0 and rng.cost == 0.0
    assert rng.total_tokens == 0
    assert rng.active_days == 0
    # The day list still spans the whole range (so a chart shows the empty gaps).
    assert rng.n_days == 3
    assert [d.date for d in rng.days] == [
        datetime.date(2026, 6, 1),
        datetime.date(2026, 6, 2),
        datetime.date(2026, 6, 3),
    ]
    assert all(d.total_tokens == 0 and d.cost == 0.0 for d in rng.days)


def test_single_day_range():
    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 13))
    rng = aggregate_range([rec(_ts(2026, 6, 13, 12), inp=42, cost=1.0)], start, end)
    assert rng.n_days == 1
    assert rng.days[0].date == datetime.date(2026, 6, 13)
    assert rng.input_tokens == 42
    assert rng.active_days == 1


# ── local-midnight boundary record lands in the right day ───────────────────────
def test_record_at_local_midnight_lands_in_that_day():
    # A record at exactly local 00:00:00 on June 16 belongs to June 16, not June 15.
    midnight = _ts(2026, 6, 16, 0, 0, 0)
    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 20))
    rng = aggregate_range([rec(midnight, inp=7, cost=0.7)], start, end)
    by_date = {d.date: d for d in rng.days}
    assert by_date[datetime.date(2026, 6, 16)].input_tokens == 7
    assert by_date[datetime.date(2026, 6, 15)].input_tokens == 0


def test_record_at_one_microsecond_to_midnight_stays_in_prior_day():
    # 23:59:59.999999 on June 15 must bucket into June 15, abutting but not crossing
    # into June 16 — the day_end_ts boundary is the last instant of the day.
    last = _ts(2026, 6, 15, 23, 59, 59, 999999)
    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 20))
    rng = aggregate_range([rec(last, inp=3)], start, end)
    by_date = {d.date: d for d in rng.days}
    assert by_date[datetime.date(2026, 6, 15)].input_tokens == 3
    assert by_date[datetime.date(2026, 6, 16)].input_tokens == 0


# ── chart empty-state distinguishes "no activity" from "activity but $0" (F4) ────
def _plain(renderable) -> str:
    """Render a Rich renderable to plain (uncolored) text for assertions."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=80, no_color=True).print(renderable)
    return buf.getvalue()


def test_range_chart_no_activity_says_no_activity():
    from cc_usage.render import range_chart
    from cc_usage.themes import get_theme

    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 13))
    rng = aggregate_range([], start, end)  # genuinely empty
    out = _plain(range_chart(rng, get_theme("dark"), "cost"))
    assert "no activity in this range" in out
    assert "peak" not in out


def test_range_chart_tokens_but_zero_cost_draws_flat_bar_not_no_activity():
    """A range with real tokens but $0 total cost (only unknown/unpriced models) is real
    activity: the COST chart must draw a (flat) bar with a `$0.00` peak label, NOT print
    'no activity in this range' (F4). The empty decision keys off record_count, not the
    cost peak being 0."""
    from cc_usage.render import range_chart
    from cc_usage.themes import get_theme

    start = day_start_ts(datetime.date(2026, 6, 13))
    end = day_end_ts(datetime.date(2026, 6, 13))
    # 1000 tokens, $0 cost, unknown model -> activity present, cost peak == 0.
    r = rec(_ts(2026, 6, 13, 12), inp=1000, cost=0.0, model="mystery", known=False)
    rng = aggregate_range([r], start, end)
    assert rng.record_count == 1 and rng.total_tokens > 0 and rng.cost == 0.0

    out = _plain(range_chart(rng, get_theme("dark"), "cost"))
    assert "no activity in this range" not in out
    assert "peak $0.00" in out  # honest $0 peak label, not a hidden chart
    # The tokens chart agrees that the range is non-empty.
    tok_out = _plain(range_chart(rng, get_theme("dark"), "tokens"))
    assert "no activity in this range" not in tok_out


# ── dataclass plumbing sanity ───────────────────────────────────────────────────
def test_dayagg_total_and_rangeagg_shape():
    d = DayAgg(date=datetime.date(2026, 6, 13), input_tokens=1, output_tokens=2, cache_tokens=3)
    assert d.total_tokens == 6
    rng = aggregate_range([], day_start_ts(datetime.date(2026, 6, 13)), day_end_ts(datetime.date(2026, 6, 13)))
    assert isinstance(rng, RangeAgg)
    assert isinstance(rng.days[0], DayAgg)
