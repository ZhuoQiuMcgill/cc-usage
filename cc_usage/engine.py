"""Data engine — owns the parser/pricing and builds RenderState snapshots.

Shared by the Textual TUI (`app.py`) and the one-shot `--once` path so the data layer
is identical in both. Wraps the T2-verified parser unchanged: a full scan on first use,
then incremental scans (M6). Each `snapshot()` re-aggregates the in-memory records for
the current `now` (cheap — ~11 ms on real data) plus the heartbeat series.

Heartbeat window/metric live in the engine so the TUI can flip them from the keyboard
and the next snapshot reflects the change immediately, without re-parsing transcripts.

Multi-account (T11/T12): the engine discovers both Claude and Codex account roots, tags
every view with the active account scope, and rolls usage up per account. Claude limits
are fetched per account; Codex limits stay a single fetch (the usage/accounting model is
what generalises, not the limits RPC). Discovery is honoured only when the user actually
configured extra roots — a plain single `~/.claude` + `~/.codex` setup hands the parser no
explicit roots, so it keeps its PROJECTS_DIR/CODEX_DIR-driven behaviour and single-account
output stays byte-identical.

Usage ledger (T17): after each scan the worker writes new/changed records to a durable,
content-free SQLite ledger, and every view is built from the live records plus the
ledger's *orphans* — rows whose key is no longer in the live parsed set because their
transcript is gone. Orphans are normally few, so memory and startup stay flat; with no
transcript deleted there are none and every view is exactly the live parse.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .accounts import (
    CLAUDE_PROVIDER,
    CODEX_PROVIDER,
    _dedupe_label,
    discover_claude_roots,
    discover_codex_roots,
    root_identity,
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
from .ledger import (
    Ledger,
    LedgerBusy,
    LedgerCorrupt,
    LedgerError,
    LedgerRow,
    orphan_record,
)
from .limits_fetch import (
    CodexAppServerUnavailable,
    LimitFetchError,
    captured_at,
    fetch_account_limits,
    fetch_codex_limits,
    load_limits_cache,
    save_limits_cache,
)
from .parser import CancelCheck, Parser, ProgressCallback, ScanCancelled, UsageRecord
from .paths import (
    LEDGER_DB,
    LIMITS_CACHE_JSON,
    PARSE_CACHE,
)
from .pricing import load_pricing
from .ratelimits import account_buckets
from .render import RenderState

# Sentinel for "put the ledger beside the parse cache" (the default).
_BESIDE_CACHE = object()
# While nothing forces a full orphan diff, re-run it at most this often when another
# process has committed to the shared ledger (it may hold history this one lacks).
_LEDGER_REDIFF_SECS = 300.0


class Engine:
    def __init__(
        self,
        config: Config,
        cache_path: Path | None = PARSE_CACHE,
        ledger_path: Path | None | object = _BESIDE_CACHE,
    ):
        self.config = config
        pricing, warns = load_pricing()
        self.warnings: list[str] = list(warns)
        # Discovered account roots — Claude (T11) and Codex (T12). Both drive scope,
        # labels, the by-account rollup and the settings root list; Claude roots also
        # drive per-account limits (Codex limits stay a single fetch, R4). Codex
        # labels are reserved against the Claude labels so the two share one scope
        # namespace.
        self.roots = discover_claude_roots(config)
        self.codex_roots = discover_codex_roots(config, claude_roots=self.roots)
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
        # Guards the copy-and-rebind of `limit_captures` and of `_worker_warnings` so the
        # UI-thread scan and the limit-refresh worker serialize their writes instead of
        # racing (T13). Readers (snapshot()/save) never take it — every writer rebinds a
        # fresh dict, so a reader only ever iterates a fully-built one. Never held across
        # a network call.
        self._limits_lock = threading.Lock()
        self.limit_warnings: list[str] = []
        # Durable usage ledger (T17). Lives beside the parse cache — so production uses
        # CONFIG_DIR/ledger.sqlite3 and a test's tmp cache gets a tmp ledger — and is off
        # when there is no cache (cache_path=None keeps tests fully in-memory). Opened
        # lazily by the first sync, which always runs on a worker thread.
        if ledger_path is _BESIDE_CACHE:
            ledger_path = cache_path.with_name(LEDGER_DB.name) if cache_path is not None else None
        self.ledger_path: Path | None = Path(ledger_path) if ledger_path is not None else None
        self._ledger = Ledger(self.ledger_path) if self.ledger_path is not None else None
        # Serializes ledger syncs (the scan worker's and the post-refresh ledger
        # worker's). Never taken on the UI thread except by close().
        self._ledger_lock = threading.Lock()
        # The parser + epoch the last full orphan diff was computed against; a different
        # parser (root swap) or epoch (records discarded) forces the next diff.
        self._ledger_synced_parser: Parser | None = None
        self._ledger_synced_epoch = -1
        self._ledger_full_at = 0.0
        self._ledger_retry = False
        self._identity_cache: tuple[tuple, dict[str, tuple[str, str, str]]] | None = None
        # Ledger-only history (T17): records whose transcripts are gone, rebuilt from the
        # ledger and priced with the current table. Rebound (never mutated) by the ledger
        # worker, so the UI thread only ever reads a complete list.
        self._orphans: list[UsageRecord] = []
        # (label, provider) of accounts present only in the ledger — roots no longer
        # configured — so the by-account rollup can still attribute their history.
        self._history_accounts: list[tuple[str, str]] = []
        # Cached live+orphan view for the UI thread (see `records`).
        self._view: tuple | None = None
        # Background-worker/timer failures (T14 R3), keyed by the step that failed. Kept
        # deliberately OUT of `limit_warnings`, which `refresh_limits` replaces wholesale —
        # a warning recorded by another step moments earlier would simply be destroyed.
        # Keying by step also bounds the surface: a failure repeating every tick stays one
        # line, and the step's own success clears it.
        self._worker_warnings: dict[str, str] = {}
        # Latched "the local codex CLI cannot serve app-server" message (T14 R4). Once set
        # it keeps reporting itself but is never retried: the refresh timer would otherwise
        # respawn a process that dies instantly, every 300 s, for the life of the session.
        self._codex_rpc_error: str | None = None
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

        Returns None — keeping the parser's legacy PROJECTS_DIR/CODEX_DIR-driven,
        hermetic behaviour (byte-identical to before) — when the machine is a plain
        single default `~/.claude` *and* single default `~/.codex` with nothing
        extra. Otherwise returns explicit roots: each enabled Claude root's projects
        tree plus each enabled Codex root's active + archived session dirs, every
        one tagged with its account label so records carry it.
        """
        claude_enabled = [r for r in self.roots if r.enabled]
        codex_enabled = [r for r in self.codex_roots if r.enabled]
        single_default_claude = len(claude_enabled) == 1 and claude_enabled[0].source == "auto"
        single_default_codex = len(codex_enabled) == 1 and codex_enabled[0].source == "auto"
        if single_default_claude and single_default_codex:
            return None
        roots: list[tuple[Path, str]] = [(r.projects, r.label) for r in claude_enabled]
        for r in codex_enabled:
            roots.append((r.path / "sessions", r.label))
            roots.append((r.path / "archived_sessions", r.label))
        return roots

    @property
    def claude_labels(self) -> list[str]:
        """Enabled Claude account labels, in discovery order."""
        return [r.label for r in self.roots if r.enabled]

    @property
    def codex_labels(self) -> list[str]:
        """Enabled Codex account labels, in discovery order (T12)."""
        return [r.label for r in self.codex_roots if r.enabled]

    @property
    def multi_account(self) -> bool:
        return len(self.claude_labels) > 1

    @property
    def multi_codex(self) -> bool:
        return len(self.codex_labels) > 1

    def _scope_accounts(self) -> list[str]:
        """Isolatable account labels (the non-`all` scope options).

        Claude accounts are always isolatable. Codex accounts join the cycle only
        when there is more than one of them: a single `~/.codex` stays lumped into
        `all` (as on the pre-T12 build), so a plain single-everything machine keeps
        its exact `all -> personal -> all` cycle — the zero-noise guarantee — while
        multiple codex roots each become their own scope."""
        accounts = list(self.claude_labels)
        if self.multi_codex:
            accounts += self.codex_labels
        return accounts

    @property
    def account_ui_active(self) -> bool:
        """Whether the account scope UI (the `a` key + scope indicator) is meaningful:
        more than one Claude account, more than one Codex account, or a single Claude
        account alongside Codex data."""
        return (
            self.multi_account
            or self.multi_codex
            or (bool(self.claude_labels) and self._codex_in_data)
        )

    def _valid_scope(self, scope: object) -> str:
        """Coerce a scope to a currently-selectable one (`all` or an isolatable
        account), so a persisted/stale scope can never strand the panel on an
        account the `a` key no longer cycles through."""
        if isinstance(scope, str) and (scope == "all" or scope in self._scope_accounts()):
            return scope
        return "all"

    @property
    def records(self) -> list[UsageRecord]:
        """Every record the views aggregate: the live parse plus ledger-only history.

        With no orphans (the normal case: no transcript has been deleted) this is the
        parser's own list, uncopied, so ledger-on output is exactly ledger-off output.
        Otherwise the orphans not (or no longer) live are appended; filtering by the live
        key set here, at read time, means a record can never be counted both live and
        from the ledger — even in the moment between a scan picking a key up again and
        the ledger worker pruning it."""
        return self._view_state()[0]

    def _view_state(self) -> tuple[list[UsageRecord], set[str]]:
        """(live + ledger-only records, unpriced model names among the ledger-only ones).

        Cached until a scan appends (the only way a key becomes live) or the orphan list
        or parser is replaced. UI/main thread only, like snapshot()."""
        parser = self.parser
        live = parser.records
        orphans = self._orphans
        if not orphans:
            return live, set()
        view = self._view
        if (
            view is not None
            and view[0] is parser
            and view[1] is live
            and view[2] == len(live)
            and view[3] is orphans
        ):
            return view[4], view[5]
        extra = [o for o in orphans if not parser.has_key(o.lkey)]
        combined = live + extra if extra else live
        unknown = {o.model_norm for o in extra if not o.known and o.model_norm != "(unknown)"}
        self._view = (parser, live, len(live), orphans, combined, unknown)
        return combined, unknown

    def _scoped_records(self) -> list[UsageRecord]:
        """Records under the active scope. "all" returns the view itself (no copy) so
        the single-account hot path is untouched; a specific account excludes Codex."""
        records = self.records
        if self.account_scope == "all":
            return records
        scope = self.account_scope
        return [r for r in records if r.account == scope]

    def cycle_account_scope(self, step: int = 1) -> str:
        """Cycle scope all -> each isolatable account -> all (Claude accounts, plus
        Codex accounts when there is more than one). No-op (and unpersisted) when the
        account UI isn't active (single Claude account, no Codex)."""
        if not self.account_ui_active:
            return self.account_scope
        options = ["all", *self._scope_accounts()]
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
        label isn't reachable live — the settings screen only toggles `enabled`.)

        Covers both providers: a toggled Codex root re-discovers and rescans through
        the same path (T12)."""
        before = [(r.label, r.enabled) for r in (*self.roots, *self.codex_roots)]
        self.roots = discover_claude_roots(self.config)
        self.codex_roots = discover_codex_roots(self.config, claude_roots=self.roots)
        if [(r.label, r.enabled) for r in (*self.roots, *self.codex_roots)] == before:
            return False
        with self._swap_lock:
            old = self.parser
            self.parser = Parser(
                old.pricing,
                cache_path=old.cache_path,
                roots=self._parser_roots(),
            )
            # Records the old parser saw but the ledger has not stored yet still go in.
            self.parser.restore_dirty(old.take_dirty())
            self._generation += 1
            self._scanned = False
            self._codex_in_data = False
            # The orphan set depends on which roots are enabled; the rescan's ledger sync
            # recomputes it against the new parser.
            self._orphans = []
            self._history_accounts = []
            self.account_scope = self._valid_scope(self.account_scope)
            # Keep the persisted config in step with the (possibly reset) scope so a
            # later save_config never writes back a scope that no longer exists.
            self.config.account_scope = self.account_scope
        return True

    def _refresh_account_flags(self) -> None:
        self._codex_in_data = any(
            r.provider == CODEX_PROVIDER for r in self.parser.records
        ) or any(r.provider == CODEX_PROVIDER for r in self._orphans)

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
            self._sync_codex_limits()

    def prime_cache(self) -> bool:
        """Expose cached aggregates immediately while reconciliation runs later."""
        if self._scanned or not self.parser.prime_cache():
            return False
        self._scanned = True
        self._refresh_account_flags()
        self._sync_codex_limits()
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

    # ── usage ledger (T17) ───────────────────────────────────────────────────
    @property
    def ledger_pending(self) -> bool:
        """Whether a ledger sync has work: changed records, a due orphan diff, or a
        failed write to retry. Cheap; the app polls it after each UI-thread scan."""
        if self._ledger is None or not self._scanned:
            return False
        parser = self.parser
        return (
            parser.has_dirty()
            or self._ledger_retry
            or self._ledger_synced_parser is not parser
            or self._ledger_synced_epoch != parser.epoch
        )

    def sync_ledger(self) -> None:
        """Write new/changed records to the ledger and refresh the ledger-only history.

        Worker threads (and `--once`) only — never the UI thread's render tick (R8).
        Never raises (T14): a busy database retries on the next scan, a full disk or
        read-only config dir runs without the ledger, and an unreadable file is renamed
        aside (never deleted) and rebuilt from the transcripts — each with a warning,
        while the panel keeps showing the in-memory data."""
        if self._ledger is None:
            return
        try:
            with self._ledger_lock:
                self._sync_ledger_locked()
        except Exception as exc:  # a bug here must still not take the panel down
            self._ledger_retry = True
            self.record_worker_warning(
                "ledger", f"usage ledger sync failed: {type(exc).__name__}: {exc}"
            )

    def _sync_ledger_locked(self) -> None:
        with self._swap_lock:
            parser = self.parser
            generation = self._generation
            scanned = self._scanned
        if not scanned:
            return  # mid root-swap: the rescan's worker syncs the new parser
        try:
            try:
                result = self._ledger_pass(parser)
            except LedgerCorrupt as exc:
                moved = self._ledger.move_aside()
                if moved is not None:
                    self.record_worker_warning(
                        "ledger moved",
                        f"usage ledger was unreadable ({exc}); moved it to {moved} "
                        "and started a fresh one from the transcripts",
                    )
                # A fresh file: the retry's full diff backfills every live record.
                self._ledger_synced_parser = None
                result = self._ledger_pass(parser)
        except LedgerBusy as exc:
            self._ledger_retry = True
            self.record_worker_warning(
                "ledger", f"usage ledger busy ({exc}); will retry on the next scan"
            )
            return
        except LedgerError as exc:
            self._ledger_retry = True
            self._ledger.close()
            self.record_worker_warning(
                "ledger", f"usage ledger unavailable ({exc}); running without it"
            )
            return
        self._ledger_retry = False
        self.record_worker_warning("ledger", None)
        if result is None:
            return
        orphans, history_accounts = result
        with self._swap_lock:
            if generation != self._generation:
                return  # the roots changed under us; the rescan recomputes this
            self._orphans = orphans
            self._history_accounts = history_accounts
            self._refresh_account_flags()

    def ledger_identities(self) -> dict[str, tuple[str, str, str]]:
        """label -> (provider, identity, label) for every discovered root, enabled or
        not. Resolving paths touches the filesystem, so it is cached per root set."""
        signature = (tuple(self.roots), tuple(self.codex_roots))
        cached = self._identity_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        out: dict[str, tuple[str, str, str]] = {}
        for root in self.roots:
            out[root.label] = (CLAUDE_PROVIDER, root_identity(root.path), root.label)
        for root in self.codex_roots:
            out[root.label] = (CODEX_PROVIDER, root_identity(root.path), root.label)
        self._identity_cache = (signature, out)
        return out

    def _ledger_row(
        self, record: UsageRecord, identities: dict[str, tuple[str, str, str]]
    ) -> LedgerRow:
        known = identities.get(record.account)
        if known is not None and known[0] == record.provider:
            _provider, identity, label = known
        else:
            # No discovered root carries this label (e.g. a legacy/embedded parser):
            # keep the row under a stable label-derived identity rather than drop it.
            identity, label = f"label:{record.account}", record.account
        return LedgerRow.from_record(record, record.provider, identity, label)

    def _ledger_pass(self, parser: Parser):
        """One sync: store changed records, then (when due) redo the orphan diff.

        Returns (orphans, history_accounts) when the ledger-only view was recomputed,
        else None. A full diff runs on the first sync of a parser, after its records
        were discarded (epoch bump), and — throttled — when another process wrote. It
        reads every stored key once: stored-but-not-live keys are the orphans, and
        live-but-not-stored records are backfilled (the first run writes them all)."""
        ledger = self._ledger
        epoch = parser.epoch
        full = self._ledger_synced_parser is not parser or self._ledger_synced_epoch != epoch
        if (
            not full
            and ledger.is_open
            and time.monotonic() - self._ledger_full_at >= _LEDGER_REDIFF_SECS
        ):
            full = ledger.changed_elsewhere()
        identities = self.ledger_identities()
        dirty = parser.take_dirty()
        try:
            if full:
                live = parser.live_index()
                stored = ledger.key_accounts()
                pending = dict(dirty)
                for key, record in live.items():
                    if key not in stored:
                        pending.setdefault(key, record)
            else:
                pending = dirty
            if pending:
                ledger.write([self._ledger_row(r, identities) for r in pending.values()])
        except BaseException:
            parser.restore_dirty(dirty)  # nothing was stored: retry these next time
            raise
        if not full:
            if self._orphans and any(parser.has_key(o.lkey) for o in self._orphans):
                orphans = [o for o in self._orphans if not parser.has_key(o.lkey)]
                return orphans, self._history_accounts
            return None
        # Keys written just now that are not live (an unstored update to a record whose
        # transcript was already gone) are orphans too.
        candidates = {key: account for key, account in stored.items() if key not in live}
        for key in pending:
            if key not in live and key not in candidates:
                candidates[key] = None
        result = self._load_orphans(parser, candidates, identities)
        self._ledger_synced_parser = parser
        self._ledger_synced_epoch = epoch
        self._ledger_full_at = time.monotonic()
        return result

    def _load_orphans(
        self,
        parser: Parser,
        candidates: dict[int, int | None],
        identities: dict[str, tuple[str, str, str]],
    ) -> tuple[list[UsageRecord], list[tuple[str, str]]]:
        """Rebuild the ledger-only records for `candidates` (key -> account id).

        Account labels (R5): a row whose root is still discovered shows under that
        root's *current* label, so a rename keeps one account; a disabled root's rows
        are excluded, like its live records; a root no longer configured falls back to
        the label stored with its rows, suffixed if a current account already uses it so
        two accounts never merge."""
        if not candidates:
            return [], []
        ledger = self._ledger
        accounts = ledger.accounts()
        models = ledger.models()
        configured = {(prov, ident): label for label, (prov, ident, _l) in identities.items()}
        enabled = {r.label for r in (*self.roots, *self.codex_roots) if r.enabled}
        used = set(identities)
        display: dict[int, tuple[str, str, bool] | None] = {}
        for account_id in sorted(accounts):
            provider, identity, stored_label = accounts[account_id]
            label = configured.get((provider, identity))
            if label is not None:
                display[account_id] = (label, provider, False) if label in enabled else None
            else:
                display[account_id] = (_dedupe_label(stored_label, used), provider, True)
        keys = [
            key
            for key, account_id in candidates.items()
            if account_id is None or display.get(account_id) is not None
        ]
        orphans: list[UsageRecord] = []
        history: dict[str, str] = {}
        pricing = parser.pricing
        for row in ledger.rows(keys):
            shown = display.get(row[1])
            if shown is None:
                continue  # a disabled root (or an unknown account id)
            label, provider, history_only = shown
            orphans.append(
                orphan_record(
                    row,
                    provider=provider,
                    label=label,
                    model_raw=models.get(row[3], ""),
                    pricing=pricing,
                )
            )
            if history_only:
                history[label] = provider
        orphans.sort(key=lambda r: r.ts)
        return orphans, sorted(history.items())

    def close(self) -> None:
        """Release the ledger connection (waits for an in-flight sync to finish)."""
        if self._ledger is None:
            return
        with self._ledger_lock:
            self._ledger.close()

    # ── heartbeat controls ───────────────────────────────────────────────────
    def refresh_limits(self) -> None:
        """Refresh each enabled Claude account's limits (network) and reconcile each
        Codex account's from its rollout snapshots (T13).

        Codex limits render straight from the parsed rollouts (see `_sync_codex_limits`,
        run after every scan); here we additionally weigh the app-server RPC, which can
        only speak for the *default* codex root's account — its login. The RPC-failure
        warning is suppressed once a snapshot already covers that account (it is noise
        when limits render from files) and surfaces only when we have nothing at all.
        Per-account isolation: one account's failure keeps its last-good capture.

        Nothing here may raise: this runs on a background worker, and an escaping
        exception tears the whole TUI down (T14). Both provider paths degrade to a
        warning, and an RPC that can never succeed in this session is asked only once."""
        enabled_claude = [r for r in self.roots if r.enabled]
        try:
            captures, warnings = fetch_account_limits(enabled_claude, self.limit_captures)
        except Exception as exc:
            # `fetch_account_limits` already isolates each account's LimitFetchError, so
            # anything escaping it is unexpected — and this runs on a worker whose
            # exceptions terminate the app (T14 R2). Keep every last-good capture, report.
            captures = dict(self.limit_captures)
            warnings = [f"Claude limit refresh failed: {exc}"]
        default_codex = next(
            (r for r in self.codex_roots if r.enabled and r.source == "auto"), None
        )
        rpc_capture: dict | None = None
        # A latched failure still warns (the user deserves to know why the RPC is gone)
        # but is never retried.
        rpc_error: str | None = self._codex_rpc_error if default_codex is not None else None
        if default_codex is not None and rpc_error is None:
            try:
                rpc_capture = fetch_codex_limits()
            except CodexAppServerUnavailable as exc:
                rpc_error = self._codex_rpc_error = str(exc)  # permanent: stop asking (R4)
            except LimitFetchError as exc:
                rpc_error = str(exc)
            except Exception as exc:
                # Belt and braces: the RPC normalizes its own failures, but an unexpected
                # escape must still degrade to a warning rather than reach the worker.
                rpc_error = f"Codex rate-limit fetch failed: {exc}"
        # Fold Codex snapshots + RPC onto the freshly-fetched Claude captures and rebind
        # `limit_captures` once, atomically — never a separate in-place write a concurrent
        # reader could catch mid-mutation.
        self._sync_codex_limits(rpc_capture=rpc_capture, base=captures)
        if rpc_error is not None and f"codex:{default_codex.label}" not in self.limit_captures:
            warnings.append(rpc_error)
        self.limit_warnings = warnings
        if self.limits_cache_path is not None:
            save_limits_cache(self.limit_captures, self.limits_cache_path)

    @property
    def worker_warnings(self) -> list[str]:
        """Current background-worker failures, in the order they were first recorded."""
        return list(self._worker_warnings.values())

    def record_worker_warning(self, label: str, message: str | None) -> None:
        """Record a worker failure under `label`, or clear it with `message=None`.

        Copy-and-rebind under the limits lock, exactly like `limit_captures`: `snapshot()`
        reads this from the UI thread while workers write it, and the scan and limits
        workers must not lose each other's updates through a read-modify-write race."""
        with self._limits_lock:
            if message is None and label not in self._worker_warnings:
                return  # nothing to clear — no needless rebind on the common success path
            warnings = dict(self._worker_warnings)
            if message is None:
                warnings.pop(label, None)
            else:
                warnings[label] = message
            self._worker_warnings = warnings

    def _sync_codex_limits(
        self, rpc_capture: dict | None = None, base: dict[str, dict] | None = None
    ) -> None:
        """Reconcile the Codex limit captures from the parser's per-root snapshots and
        rebind `self.limit_captures` to a fresh dict (T13). No network.

        Runs after every scan/warm prime so codex limits render from the rollouts with no
        fetch; `refresh_limits` passes the just-fetched Claude captures as `base` plus the
        app-server RPC. Precedence per account: the freshest of {live snapshot, last-good
        capture, and — for the default root only — the RPC} wins; a non-default root can't
        be attributed to the local RPC. A codex capture for a root no longer enabled is
        pruned so it neither lingers in the persisted file nor skews `rl_present`.

        The dict is copied, updated, and rebound in one locked step rather than mutated in
        place: readers on other threads (`snapshot()`'s rl_present scan, the limits-cache
        save) only ever iterate a fully-built dict, and concurrent writers (a UI-thread
        scan vs. the limit-refresh worker) serialize instead of losing updates. The lock
        never spans a network call."""
        labels = self.codex_labels
        enabled_keys = {f"codex:{label}" for label in labels}
        default_label = next(
            (r.label for r in self.codex_roots if r.enabled and r.source == "auto"), None
        )
        snapshots = self.parser.latest_rate_limits_by_account
        with self._limits_lock:
            captures = dict(self.limit_captures if base is None else base)
            for name in [
                n for n in captures if n.startswith("codex:") and n not in enabled_keys
            ]:
                del captures[name]
            for label in labels:
                candidates: list[dict] = []
                snapshot = snapshots.get(label)
                if snapshot is not None:
                    candidates.append(snapshot)
                prior = captures.get(f"codex:{label}")
                if prior is not None:
                    candidates.append(prior)
                if rpc_capture is not None and label == default_label:
                    candidates.append(rpc_capture)
                if candidates:
                    captures[f"codex:{label}"] = max(candidates, key=captured_at)
            self.limit_captures = captures

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
        codex_labels = self.codex_labels
        buckets = account_buckets(
            self.limit_captures,
            labels,
            codex_labels,
            multi_claude=self.multi_account,
            multi_codex=self.multi_codex,
        )
        hb = series(records, now, self.hb_window, self.hb_metric)
        # By-account rollup (R4) only in "all" scope, and only when it adds information
        # (>=2 rows: several accounts, or Codex data alongside a Claude account).
        accounts = []
        if self.account_scope == "all":
            rollup = aggregate_accounts(
                self.records,
                now,
                self.config.default_window,
                labels,
                codex_labels,
                history_accounts=self._history_accounts,
            )
            if len(rollup) >= 2:
                accounts = rollup
        return RenderState(
            windows=windows,
            buckets=buckets,
            now=now,
            config=self.config,
            interval=self.config.refresh_interval,
            # Only per-account captures (`claude:<label>`, `codex:<label>`) are
            # renderable by account_buckets; a stray legacy bare `claude`/`codex` key
            # must not make the panel claim usable provider data it cannot show.
            rl_present=any(
                capture and (name.startswith("codex:") or name.startswith("claude:"))
                for name, capture in self.limit_captures.items()
            ),
            unknown_models=set(self.parser.stats.unknown_models) | self._view_state()[1],
            warnings=[*self.warnings, *self.limit_warnings, *self.worker_warnings],
            heartbeat=hb,
            accounts=accounts,
            account_scope=self.account_scope,
            # The scope-line "(…)" hint lists exactly the accounts `a` cycles through
            # (Claude labels, plus Codex labels once there is more than one codex root).
            account_names=self._scope_accounts(),
            account_ui=self.account_ui_active,
            # The exact table the parser priced the records with, so the Models board's
            # $/M columns show the rates behind the Cost column (T16).
            pricing=self.parser.pricing,
        )
