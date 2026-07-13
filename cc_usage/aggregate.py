"""Rolling-window aggregation (T0 §5, T3 R1) + the heartbeat series (T3 R2).

Per-model token sums + cost for the last 1h / 5h / 24h / **7d** and all-time.
Windows are *rolling* (`now - timestamp <= window`), which is pure epoch math and
therefore timezone-independent — the "local time" note in T0 §5 only matters for
calendar buckets, which we are explicitly not doing.

Recomputed cheaply from the in-memory record list each data refresh, so the
windows stay correct as `now` advances without re-parsing transcripts (M6).

`series(records, now, window, metric)` (T3 R2) produces the compact "heartbeat":
each point is the **rate per time bucket** — the sum of cost (or tokens) for the
records that fall inside that bucket — *not* a cumulative running total. The window
is split into ~`_SERIES_BUCKETS` equal buckets; an empty window yields all-zeros
(the renderer shows a flat line / "no activity", never crashes).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .parser import UsageRecord

# name -> window length in seconds (None = all-time). Order is display order.
# T3 R1 adds the rolling last-7-days (168h) column between 24h and all-time.
WINDOWS: tuple[tuple[str, int | None], ...] = (
    ("1h", 3600),
    ("5h", 5 * 3600),
    ("24h", 24 * 3600),
    ("7d", 7 * 24 * 3600),
    ("all", None),
)
WINDOW_NAMES = [name for name, _ in WINDOWS]

# Heartbeat (T3 R2): the three switchable windows and their span in seconds.
HEARTBEAT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("5h", 5 * 3600),
    ("24h", 24 * 3600),
    ("7d", 7 * 24 * 3600),
)
HEARTBEAT_WINDOW_SECS = dict(HEARTBEAT_WINDOWS)
HEARTBEAT_METRICS = ("cost", "tokens")
# Bucket count for the sparkline. ~48 keeps it in the 40–60 band the spec asks for
# and reads well in a compact braille strip (2 samples per braille cell -> 24 cells).
_SERIES_BUCKETS = 48


@dataclass
class ModelAgg:
    model: str  # normalized id
    known: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens


@dataclass
class WindowAgg:
    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0
    unpriced_tokens: int = 0
    models: dict[str, ModelAgg] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens

    @property
    def pricing_coverage(self) -> float:
        """Fraction of tokens whose model has a price (1.0 for an empty window)."""
        if self.total_tokens == 0:
            return 1.0
        return max(0.0, 1.0 - self.unpriced_tokens / self.total_tokens)

    def _add(self, r: UsageRecord) -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cache_tokens += r.cache_tokens
        self.cost += r.cost
        if not r.known:
            self.unpriced_tokens += r.total_tokens
        m = self.models.get(r.model_norm)
        if m is None:
            m = ModelAgg(model=r.model_norm, known=r.known)
            self.models[r.model_norm] = m
        m.input_tokens += r.input_tokens
        m.output_tokens += r.output_tokens
        m.cache_tokens += r.cache_tokens
        m.cost += r.cost
        m.known = m.known and r.known

    def models_sorted(self) -> list[ModelAgg]:
        """Per-model rows, biggest cost first (then tokens) for the table."""
        return sorted(
            self.models.values(),
            key=lambda m: (m.cost, m.total_tokens),
            reverse=True,
        )


def aggregate(records: list[UsageRecord], now: float) -> dict[str, WindowAgg]:
    """Bucket every record into each rolling window it falls inside."""
    out = {name: WindowAgg(name=name) for name, _ in WINDOWS}
    for r in records:
        age = now - r.ts
        for name, secs in WINDOWS:
            if secs is None or age <= secs:
                out[name]._add(r)
    return out


@dataclass
class Series:
    """A heartbeat sample strip: rate-per-bucket values + display metadata.

    `values[i]` is the summed `metric` for records whose timestamp lands in bucket i,
    bucket 0 = oldest edge of the window, bucket -1 = the one ending at `now`. Always
    `_SERIES_BUCKETS` long. `peak` is the max bucket value (0.0 when the window is empty).
    """

    window: str  # "5h" | "24h" | "7d"
    metric: str  # "cost" | "tokens"
    values: list[float]
    peak: float
    bucket_seconds: float
    window_seconds: int
    now: float
    record_count: int = 0
    unpriced_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        return self.record_count == 0


def series(
    records: list[UsageRecord],
    now: float,
    window: str = "24h",
    metric: str = "cost",
    buckets: int = _SERIES_BUCKETS,
) -> Series:
    """Rate-per-bucket heartbeat for `window`/`metric` (T3 R2).

    Each returned value is the *sum* of the chosen metric over the records inside that
    bucket (not a cumulative total). Records outside the window are ignored. An empty
    window returns an all-zeros series (peak 0.0) so the renderer can show "no activity".
    Unknown window/metric fall back to the defaults rather than crashing (Rulebook r4).
    """
    if metric not in HEARTBEAT_METRICS:
        metric = "cost"
    secs = HEARTBEAT_WINDOW_SECS.get(window)
    if secs is None:
        window, secs = "24h", HEARTBEAT_WINDOW_SECS["24h"]
    buckets = max(1, int(buckets))
    bucket_seconds = secs / buckets

    vals = [0.0] * buckets
    record_count = 0
    unpriced_tokens = 0
    start = now - secs  # left edge of the oldest bucket
    for r in records:
        if r.ts <= start or r.ts > now:
            # Left edge is exclusive / right edge inclusive so a record exactly at
            # `now` lands in the final bucket and nothing is double-placed.
            continue
        idx = int((r.ts - start) / bucket_seconds)
        if idx >= buckets:  # guards the r.ts == now boundary
            idx = buckets - 1
        vals[idx] += r.cost if metric == "cost" else float(r.total_tokens)
        record_count += 1
        if not r.known:
            unpriced_tokens += r.total_tokens

    peak = max(vals) if vals else 0.0
    return Series(
        window=window,
        metric=metric,
        values=vals,
        peak=peak,
        bucket_seconds=bucket_seconds,
        window_seconds=secs,
        now=now,
        record_count=record_count,
        unpriced_tokens=unpriced_tokens,
    )


# ── Date-range analysis (T7) ───────────────────────────────────────────────────
# Unlike the rolling windows above (pure, timezone-independent epoch math), a
# *date range* — "June 13 → June 20" — is a human **calendar** concept. So this
# whole section deliberately works in the machine's LOCAL timezone:
#   * a record's day is `datetime.fromtimestamp(r.ts).date()` (local civil day),
#   * the picked start/end *dates* are turned into epoch bounds at LOCAL midnight
#     / local 23:59:59.999999 via day_start_ts / day_end_ts.
# The inclusion test itself is still plain epoch math (start_ts <= r.ts <= end_ts);
# only the *derivation* of those bounds and the per-day bucketing are local-time.
# Tests pin TZ (set TZ + time.tzset()) so this is deterministic.


def day_start_ts(date: datetime.date) -> float:
    """Epoch seconds at LOCAL 00:00:00 on `date` (inclusive lower day bound)."""
    return datetime.datetime(date.year, date.month, date.day, 0, 0, 0).timestamp()


def day_end_ts(date: datetime.date) -> float:
    """Epoch seconds at LOCAL 23:59:59.999999 on `date` (inclusive upper day bound).

    The last representable instant of the day, so `[day_start_ts(d) .. day_end_ts(d)]`
    is a fully inclusive single-day range that abuts — but never overlaps — the next
    day's 00:00:00.
    """
    return datetime.datetime(
        date.year, date.month, date.day, 23, 59, 59, 999999
    ).timestamp()


@dataclass
class DayAgg:
    """One LOCAL calendar day's usage (zero-filled for days with no activity)."""

    date: datetime.date  # local calendar day
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0
    unpriced_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens

    def _add(self, r: UsageRecord) -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cache_tokens += r.cache_tokens
        self.cost += r.cost
        if not r.known:
            self.unpriced_tokens += r.total_tokens


@dataclass
class RangeAgg:
    """Usage over an inclusive [start_ts, end_ts] calendar range (T7).

    `models` mirrors `WindowAgg.models` (so `model_block`-style renderers work). `days`
    holds one `DayAgg` PER local calendar day from the start date to the end date
    inclusive — including zero-usage days — chronologically, so charts/tables show gaps.
    """

    start_ts: float  # inclusive lower bound (epoch seconds)
    end_ts: float  # inclusive upper bound (epoch seconds)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0
    unpriced_tokens: int = 0
    record_count: int = 0
    models: dict[str, ModelAgg] = field(default_factory=dict)
    days: list[DayAgg] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens

    @property
    def pricing_coverage(self) -> float:
        if self.total_tokens == 0:
            return 1.0
        return max(0.0, 1.0 - self.unpriced_tokens / self.total_tokens)

    @property
    def n_days(self) -> int:
        return len(self.days)

    @property
    def active_days(self) -> int:
        """Calendar days in range with any usage (tokens or cost)."""
        return sum(1 for d in self.days if d.total_tokens > 0 or d.cost > 0.0)

    def _add_model(self, r: UsageRecord) -> None:
        m = self.models.get(r.model_norm)
        if m is None:
            m = ModelAgg(model=r.model_norm, known=r.known)
            self.models[r.model_norm] = m
        m.input_tokens += r.input_tokens
        m.output_tokens += r.output_tokens
        m.cache_tokens += r.cache_tokens
        m.cost += r.cost
        m.known = m.known and r.known

    def models_sorted(self) -> list[ModelAgg]:
        """Per-model rows, biggest cost first (then tokens) — same order as WindowAgg."""
        return sorted(
            self.models.values(),
            key=lambda m: (m.cost, m.total_tokens),
            reverse=True,
        )


def aggregate_range(
    records: list[UsageRecord], start_ts: float, end_ts: float
) -> RangeAgg:
    """Aggregate every record inside the **inclusive** [start_ts, end_ts] range (T7).

    Inclusion is `start_ts <= r.ts <= end_ts` — inclusive at *both* ends, unlike the
    rolling-window helper (which is left-exclusive). That is intentional: a date range
    is explicit calendar bounds, so the user expects records exactly on either edge to
    count. Per-day bucketing uses the LOCAL calendar day of each record. `days` spans
    every calendar day from start to end inclusive, zero-filled, so a per-day table or
    chart shows the gaps. Unknown-model records still count and track unpriced-token
    coverage just as the rolling aggregation does — never crash (Rulebook r4).
    """
    rng = RangeAgg(start_ts=start_ts, end_ts=end_ts)

    # Pre-build one DayAgg per local calendar day in the (possibly empty) range, so
    # zero days exist even when no record lands in them. If start > end (shouldn't
    # happen — the screen keeps start<=end) we yield an empty day list, not a crash.
    start_date = datetime.datetime.fromtimestamp(start_ts).date()
    end_date = datetime.datetime.fromtimestamp(end_ts).date()
    by_date: dict[datetime.date, DayAgg] = {}
    d = start_date
    while d <= end_date:
        day = DayAgg(date=d)
        rng.days.append(day)
        by_date[d] = day
        d += datetime.timedelta(days=1)

    for r in records:
        if r.ts < start_ts or r.ts > end_ts:
            continue
        rng.input_tokens += r.input_tokens
        rng.output_tokens += r.output_tokens
        rng.cache_tokens += r.cache_tokens
        rng.cost += r.cost
        if not r.known:
            rng.unpriced_tokens += r.total_tokens
        rng.record_count += 1
        rng._add_model(r)
        # Local civil day of the record — the date-range concept is calendar-local.
        rec_date = datetime.datetime.fromtimestamp(r.ts).date()
        day = by_date.get(rec_date)
        if day is not None:
            day._add(r)
        # A record inside [start_ts, end_ts] always maps to a date within
        # [start_date, end_date], so `day` is never None in practice; the guard just
        # keeps a pathological clock value from crashing the per-day bucketing.

    return rng
