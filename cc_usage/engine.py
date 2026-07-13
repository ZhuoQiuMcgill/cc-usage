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
from .limits_fetch import (
    fetch_provider_limits,
    load_limits_cache,
    save_limits_cache,
)
from .parser import CancelCheck, Parser, ProgressCallback
from .paths import LIMITS_CACHE_JSON, PARSE_CACHE
from .pricing import load_pricing
from .ratelimits import provider_buckets
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
        self.limits_cache_path = LIMITS_CACHE_JSON if cache_path is not None else None
        self.limit_captures = (
            load_limits_cache(self.limits_cache_path)
            if self.limits_cache_path is not None
            else {}
        )
        self.limit_warnings: list[str] = []
        self.last_scan_at: float | None = None
        self.last_scan_seconds: float | None = None
        # Heartbeat view state (T3 R2). Default window 24h, default metric cost.
        self.hb_window = "24h"
        self.hb_metric = "cost"

    # ── data ───────────────────────────────────────────────────────────────
    @property
    def is_scanned(self) -> bool:
        return self._scanned

    def scan(
        self,
        progress: ProgressCallback | None = None,
        cancelled: CancelCheck | None = None,
    ) -> None:
        """Read new transcript lines, optionally reporting progress/cancellation."""
        started = time.perf_counter()
        self.parser.scan(progress=progress, cancelled=cancelled)
        self.last_scan_seconds = time.perf_counter() - started
        self.last_scan_at = time.time()
        self._scanned = True

    def prime_cache(self) -> bool:
        """Expose cached aggregates immediately while reconciliation runs later."""
        if self._scanned or not self.parser.prime_cache():
            return False
        self._scanned = True
        return True

    def save_cache(self) -> None:
        """Persist the parser's state so the next launch starts warm (no-op if the
        engine was built with cache_path=None, e.g. in tests)."""
        self.parser.save_cache()

    def ensure_scanned(self) -> None:
        if not self._scanned:
            self.scan()

    # ── heartbeat controls ───────────────────────────────────────────────────
    def refresh_limits(self) -> None:
        """Fetch current Claude and Codex limits; retain last good values on errors."""
        captures, warnings = fetch_provider_limits(self.limit_captures)
        if "codex" not in captures and self.parser.latest_rate_limits is not None:
            captures["codex"] = self.parser.latest_rate_limits
            warnings.append("Codex limits are last-observed local values")
        self.limit_captures = captures
        self.limit_warnings = warnings
        if self.limits_cache_path is not None:
            save_limits_cache(self.limit_captures, self.limits_cache_path)

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
        claude_limits = self.limit_captures.get("claude")
        codex_limits = self.limit_captures.get("codex")
        buckets = provider_buckets(claude_limits, codex_limits)
        hb = series(self.parser.records, now, self.hb_window, self.hb_metric)
        return RenderState(
            windows=windows,
            buckets=buckets,
            now=now,
            config=self.config,
            interval=self.config.refresh_interval,
            rl_present=claude_limits is not None or codex_limits is not None,
            unknown_models=set(self.parser.stats.unknown_models),
            warnings=[*self.warnings, *self.limit_warnings],
            heartbeat=hb,
        )
