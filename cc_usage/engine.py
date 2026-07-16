"""Data engine — owns the parser/pricing and builds RenderState snapshots.

Shared by the Textual TUI (`app.py`) and the one-shot `--once` path so the data layer
is identical in both. Wraps the T2-verified parser unchanged: a full scan on first use,
then incremental scans (M6). Each `snapshot()` re-aggregates the in-memory records for
the current `now` (cheap — ~11 ms on real data) plus the heartbeat series.

Heartbeat window/metric live in the engine so the TUI can flip them from the keyboard
and the next snapshot reflects the change immediately, without re-parsing transcripts.

Multi-account (T11): the engine discovers Claude account roots, tags every view with the
active account scope, rolls usage up per account, and fetches subscription limits per
account. Discovery is honoured only when the user actually configured extra roots — a
plain single `~/.claude` setup hands the parser no explicit roots, so it keeps its
PROJECTS_DIR-driven behaviour and single-account output stays byte-identical.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .accounts import (
    CODEX_ACCOUNT,
    discover_claude_roots,
)
from .aggregate import (
    HEARTBEAT_METRICS,
    HEARTBEAT_WINDOW_SECS,
    RangeAgg,
    aggregate,
    aggregate_accounts,
    aggregate_range,
    series,
)
from .config import Config, save_config
from .limits_fetch import (
    fetch_account_limits,
    load_limits_cache,
    save_limits_cache,
)
from .parser import CancelCheck, Parser, ProgressCallback, ScanCancelled, UsageRecord
from .paths import (
    CODEX_ARCHIVED_SESSIONS_DIR,
    CODEX_SESSIONS_DIR,
    LIMITS_CACHE_JSON,
    PARSE_CACHE,
)
from .pricing import load_pricing
from .ratelimits import account_buckets
from .render import RenderState


class Engine:
    def __init__(self, config: Config, cache_path: Path | None = PARSE_CACHE):
        self.config = config
        pricing, warns = load_pricing()
        self.warnings: list[str] = list(warns)
        # Discovered Claude account roots (T11) — drives scope, labels, the by-account
        # rollup, per-account limits, and the settings root list.
        self.roots = discover_claude_roots(config)
        # cache_path defaults to the real persistent cache for the app/CLI; tests pass
        # cache_path=None to stay fully in-memory and hermetic.
        self.parser = Parser(pricing, cache_path=cache_path, roots=self._parser_roots())
        self._scanned = False
        self._persist = cache_path is not None
        # Root-swap guard (T11). `reload_roots()` swaps in a fresh parser; a scan of the
        # old parser may still be running on a worker thread. The generation counter
        # lets that stale scan detect the swap and discard itself, and the lock makes
        # the check-and-publish of scan results atomic against the swap, so a stale
        # worker can never mark the fresh (empty) parser as scanned or persist it.
        self._generation = 0
        self._swap_lock = threading.Lock()
        self.limits_cache_path = LIMITS_CACHE_JSON if cache_path is not None else None
        self.limit_captures = (
            load_limits_cache(self.limits_cache_path)
            if self.limits_cache_path is not None
            else {}
        )
        self.limit_warnings: list[str] = []
        self.last_scan_at: float | None = None
        self.last_scan_seconds: float | None = None
        # Cached "does any Codex usage exist" flag (recomputed each scan) so the account
        # scope UI can activate for a single Claude account + Codex without an O(n) walk
        # on every key press.
        self._codex_in_data = False
        # Active account scope: "all" or a Claude account label (validated live).
        self.account_scope = self._valid_scope(config.account_scope)
        # Heartbeat view state (T3 R2). Default window 24h, default metric cost.
        self.hb_window = "24h"
        self.hb_metric = "cost"

    # ── accounts ─────────────────────────────────────────────────────────────
    def _parser_roots(self) -> list[tuple[Path, str]] | None:
        """Roots to hand the parser.

        Returns None — keeping the parser's legacy PROJECTS_DIR-driven, hermetic
        behaviour — when the only enabled root is the auto `~/.claude`. Otherwise
        returns explicit Claude roots plus the Codex dirs so both providers are
        scanned and each record is tagged with its account.
        """
        enabled = [r for r in self.roots if r.enabled]
        if len(enabled) == 1 and enabled[0].source == "auto":
            return None
        roots: list[tuple[Path, str]] = [(r.projects, r.label) for r in enabled]
        roots.append((CODEX_SESSIONS_DIR, CODEX_ACCOUNT))
        roots.append((CODEX_ARCHIVED_SESSIONS_DIR, CODEX_ACCOUNT))
        return roots

    @property
    def claude_labels(self) -> list[str]:
        """Enabled Claude account labels, in discovery order."""
        return [r.label for r in self.roots if r.enabled]

    @property
    def multi_account(self) -> bool:
        return len(self.claude_labels) > 1

    @property
    def account_ui_active(self) -> bool:
        """Whether the account scope UI (the `a` key + scope indicator) is meaningful:
        more than one Claude account, or a single Claude account alongside Codex data."""
        return self.multi_account or (bool(self.claude_labels) and self._codex_in_data)

    def _valid_scope(self, scope: object) -> str:
        if isinstance(scope, str) and (scope == "all" or scope in self.claude_labels):
            return scope
        return "all"

    def _scoped_records(self) -> list[UsageRecord]:
        """Records under the active scope. "all" returns the list itself (no copy) so
        the single-account hot path is untouched; a specific account excludes Codex."""
        if self.account_scope == "all":
            return self.parser.records
        scope = self.account_scope
        return [r for r in self.parser.records if r.account == scope]

    def cycle_account_scope(self, step: int = 1) -> str:
        """Cycle scope all -> each Claude account -> all. No-op (and unpersisted) when
        the account UI isn't active (single Claude account, no Codex)."""
        if not self.account_ui_active:
            return self.account_scope
        options = ["all", *self.claude_labels]
        current = self._valid_scope(self.account_scope)
        self.account_scope = options[(options.index(current) + step) % len(options)]
        self.config.account_scope = self.account_scope
        if self._persist:
            try:
                save_config(self.config)
            except OSError:
                pass
        return self.account_scope

    def reload_roots(self) -> bool:
        """Re-discover roots after a settings change; rebuild the parser and force a
        fresh scan when the enabled set changed. Returns True if it changed (so the
        caller can relaunch a scan). The swap bumps the scan generation, so any
        in-flight scan of the old parser discards itself instead of publishing
        stale/empty state, and the cache's root fingerprint invalidates old on-disk
        state — a disabled root's records drop after the rescan. (Change detection
        compares (label, enabled) pairs; a pure path change under an unchanged
        label isn't reachable live — the settings screen only toggles `enabled`.)"""
        before = [(r.label, r.enabled) for r in self.roots]
        self.roots = discover_claude_roots(self.config)
        if [(r.label, r.enabled) for r in self.roots] == before:
            return False
        with self._swap_lock:
            self.parser = Parser(
                self.parser.pricing,
                cache_path=self.parser.cache_path,
                roots=self._parser_roots(),
            )
            self._generation += 1
            self._scanned = False
            self._codex_in_data = False
            self.account_scope = self._valid_scope(self.account_scope)
            # Keep the persisted config in step with the (possibly reset) scope so a
            # later save_config never writes back a scope that no longer exists.
            self.config.account_scope = self.account_scope
        return True

    def _refresh_account_flags(self) -> None:
        self._codex_in_data = any(r.account == CODEX_ACCOUNT for r in self.parser.records)

    # ── data ───────────────────────────────────────────────────────────────
    @property
    def is_scanned(self) -> bool:
        return self._scanned

    def scan(
        self,
        progress: ProgressCallback | None = None,
        cancelled: CancelCheck | None = None,
    ) -> None:
        """Read new transcript lines, optionally reporting progress/cancellation.

        The parser and root generation are captured at entry: if `reload_roots()`
        swaps the parser while this scan runs (a Settings root toggle mid-scan),
        the stale result is discarded by raising ScanCancelled rather than marking
        the fresh, empty parser as scanned — which would blank the panel and let
        the worker persist an empty cache."""
        started = time.perf_counter()
        with self._swap_lock:
            # Capture the pair atomically: read outside the lock, a swap landing
            # between the two reads could pair the old parser with the new
            # generation and slip past the staleness check below.
            parser = self.parser
            generation = self._generation
        parser.scan(progress=progress, cancelled=cancelled)
        with self._swap_lock:
            if generation != self._generation:
                raise ScanCancelled("account roots changed during the scan")
            self.last_scan_seconds = time.perf_counter() - started
            self.last_scan_at = time.time()
            self._scanned = True
            self._refresh_account_flags()

    def prime_cache(self) -> bool:
        """Expose cached aggregates immediately while reconciliation runs later."""
        if self._scanned or not self.parser.prime_cache():
            return False
        self._scanned = True
        self._refresh_account_flags()
        return True

    def save_cache(self) -> None:
        """Persist the parser's state so the next launch starts warm (no-op if the
        engine was built with cache_path=None, e.g. in tests).

        Skipped when nothing is scanned: after a mid-scan root swap the current
        parser is fresh and empty, and persisting it would poison the warm-start
        cache with zero records under the new root fingerprint."""
        with self._swap_lock:
            if not self._scanned:
                return
            parser = self.parser
        parser.save_cache()

    def ensure_scanned(self) -> None:
        if not self._scanned:
            self.scan()

    # ── heartbeat controls ───────────────────────────────────────────────────
    def refresh_limits(self) -> None:
        """Fetch each enabled Claude account's + Codex limits; retain last good values
        on errors (per account, isolated). Single-account behaviour is unchanged."""
        enabled = [r for r in self.roots if r.enabled]
        captures, warnings = fetch_account_limits(enabled, self.limit_captures)
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
        Honours the active account scope (T11 R3).
        """
        self.ensure_scanned()
        return aggregate_range(self._scoped_records(), start_ts, end_ts)

    # ── snapshot ─────────────────────────────────────────────────────────────
    def snapshot(self, now: float | None = None) -> RenderState:
        if now is None:
            now = time.time()
        self.ensure_scanned()
        records = self._scoped_records()
        windows = aggregate(records, now)
        labels = self.claude_labels
        buckets = account_buckets(self.limit_captures, labels, multi=self.multi_account)
        hb = series(records, now, self.hb_window, self.hb_metric)
        # By-account rollup (R4) only in "all" scope, and only when it adds information
        # (>=2 rows: several Claude accounts, or Codex data alongside a Claude account).
        accounts = []
        if self.account_scope == "all":
            rollup = aggregate_accounts(
                self.parser.records, now, self.config.default_window, labels
            )
            if len(rollup) >= 2:
                accounts = rollup
        return RenderState(
            windows=windows,
            buckets=buckets,
            now=now,
            config=self.config,
            interval=self.config.refresh_interval,
            # Only account-keyed Claude captures (`claude:<label>`) and `codex` are
            # renderable by account_buckets; a stray legacy bare `claude` key must not
            # make the panel claim usable provider data it cannot show.
            rl_present=any(
                capture and (name == "codex" or name.startswith("claude:"))
                for name, capture in self.limit_captures.items()
            ),
            unknown_models=set(self.parser.stats.unknown_models),
            warnings=[*self.warnings, *self.limit_warnings],
            heartbeat=hb,
            accounts=accounts,
            account_scope=self.account_scope,
            account_names=labels,
            account_ui=self.account_ui_active,
        )
