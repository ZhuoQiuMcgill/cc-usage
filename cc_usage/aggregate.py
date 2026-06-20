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
    models: dict[str, ModelAgg] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens

    def _add(self, r: UsageRecord) -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cache_tokens += r.cache_tokens
        self.cost += r.cost
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

    @property
    def is_empty(self) -> bool:
        return self.peak <= 0.0


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

    peak = max(vals) if vals else 0.0
    return Series(
        window=window,
        metric=metric,
        values=vals,
        peak=peak,
        bucket_seconds=bucket_seconds,
        window_seconds=secs,
        now=now,
    )
