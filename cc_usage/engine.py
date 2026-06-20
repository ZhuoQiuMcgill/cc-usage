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

from .aggregate import HEARTBEAT_METRICS, HEARTBEAT_WINDOW_SECS, aggregate, series
from .config import Config
from .parser import Parser
from .pricing import load_pricing
from .ratelimits import get_buckets, load_ratelimits
from .render import RenderState


class Engine:
    def __init__(self, config: Config):
        self.config = config
        pricing, warns = load_pricing()
        self.warnings: list[str] = list(warns)
        self.parser = Parser(pricing)
        self._scanned = False
        # Heartbeat view state (T3 R2). Default window 24h, default metric cost.
        self.hb_window = "24h"
        self.hb_metric = "cost"

    # ── data ───────────────────────────────────────────────────────────────
    def scan(self) -> None:
        """Read new transcript lines (full on first call, incremental after)."""
        self.parser.scan()
        self._scanned = True

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
