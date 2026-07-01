"""Data engine — owns the parser/pricing and builds RenderState snapshots.

Shared by the Textual TUI (`app.py`) and the one-shot `--once` path so the data layer
is identical in both. Wraps the T2-verified parser unchanged: a full scan on first use,
then incremental scans (M6). Each `snapshot()` re-aggregates the in-memory records for
the current `now` (cheap — ~11 ms on real data) plus the heartbeat series.

Heartbeat window/metric live in the engine so the TUI can flip them from the keyboard
and the next snapshot reflects the change immediately, without re-parsing transcripts.
"""

from __future__ import annotations

import time

from .aggregate import (
    HEARTBEAT_METRICS,
    HEARTBEAT_WINDOW_SECS,
    RangeAgg,
    aggregate,
    aggregate_range,
    series,
)
from pathlib import Path

from .config import Config
from .parser import Parser
from .paths import PARSE_CACHE
from .pricing import load_pricing
from .ratelimits import get_buckets, load_ratelimits
from .render import RenderState


class Engine:
    def __init__(self, config: Config, cache_path: Path | None = PARSE_CACHE):
        self.config = config
        pricing, warns = load_pricing()
        self.warnings: list[str] = list(warns)
        # cache_path defaults to the real persistent cache for the app/CLI; tests pass
        # cache_path=None to stay fully in-memory and hermetic.
        self.parser = Parser(pricing, cache_path=cache_path)
        self._scanned = False
        # Heartbeat view state (T3 R2). Default window 24h, default metric cost.
        self.hb_window = "24h"
        self.hb_metric = "cost"

    # ── data ───────────────────────────────────────────────────────────────
    @property
    def is_scanned(self) -> bool:
        return self._scanned

    def scan(self) -> None:
        """Read new transcript lines (full on first call, incremental after)."""
        self.parser.scan()
        self._scanned = True

    def save_cache(self) -> None:
        """Persist the parser's state so the next launch starts warm (no-op if the
        engine was built with cache_path=None, e.g. in tests)."""
        self.parser.save_cache()

    def ensure_scanned(self) -> None:
        if not self._scanned:
            self.scan()

    # ── heartbeat controls ───────────────────────────────────────────────────
    def cycle_hb_window(self, step: int = 1) -> str:
        names = list(HEARTBEAT_WINDOW_SECS.keys())
        i = (names.index(self.hb_window) + step) % len(names)
        self.hb_window = names[i]
        return self.hb_window

    def toggle_hb_metric(self) -> str:
        i = (HEARTBEAT_METRICS.index(self.hb_metric) + 1) % len(HEARTBEAT_METRICS)
        self.hb_metric = HEARTBEAT_METRICS[i]
        return self.hb_metric

    # ── date-range analysis (T7) ──────────────────────────────────────────────
    def range_metrics(self, start_ts: float, end_ts: float) -> RangeAgg:
        """Aggregate the in-memory records over an inclusive [start_ts, end_ts] range.

        Kept deliberately separate from snapshot()/heartbeat state (no `hb_window`
        entanglement): the date-range view is its own thing, computed on demand from the
        same already-parsed records. Reads nothing new from disk beyond the initial scan.
        """
        self.ensure_scanned()
        return aggregate_range(self.parser.records, start_ts, end_ts)

    # ── snapshot ─────────────────────────────────────────────────────────────
    def snapshot(self, now: float | None = None) -> RenderState:
        if now is None:
            now = time.time()
        self.ensure_scanned()
        windows = aggregate(self.parser.records, now)
        rl = load_ratelimits()
        hb = series(self.parser.records, now, self.hb_window, self.hb_metric)
        return RenderState(
            windows=windows,
            buckets=get_buckets(rl),
            now=now,
            config=self.config,
            interval=self.config.refresh_interval,
            rl_present=rl is not None,
            unknown_models=set(self.parser.stats.unknown_models),
            warnings=list(self.warnings),
            heartbeat=hb,
        )
