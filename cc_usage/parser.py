"""Transcript parser (T0 §4A) — dedup, tolerant extraction, incremental reads.

Scans ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl. For each *assistant*
record that carries a usage object it keeps one UsageRecord per unique
(requestId, message.id). Claude Code streams a single assistant reply across
several transcript lines that share that key while only output_tokens grows (the
first line is a partial message_start snapshot; the last carries the final
counts), so repeat lines are *merged* into the kept record — field-wise max of the
counters, cost recomputed — rather than dropped as duplicates, which would throw
away the final (priciest) output tokens (T9). Retries/sidechain echoes still
collapse to one record. Sidechain / subagent usage is included (it is real spend).
Anything malformed or non-usage is skipped, never fatal (Rulebook rule 4).

Reads are incremental (M6 / T0 §9): each file's byte offset + size + mtime are
remembered, and on a later scan only newly appended *complete* lines are read. That
state can also be *persisted* across process runs (a stale/corrupt cache is ignored),
so a relaunch re-parses only bytes appended since the last run — see `load_cache` /
`save_cache`.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .accounts import CODEX_ACCOUNT, DEFAULT_LABEL
from .cost import compute_cost, get_rates, normalize_model
from .paths import (
    CODEX_ARCHIVED_SESSIONS_DIR,
    CODEX_SESSIONS_DIR,
    PROJECTS_DIR,
)

# Optional fast JSON. orjson (a C extension) parses bytes directly and is ~2x the
# stdlib on this workload; it's NOT a hard dependency, so we use it only if the user
# happens to have it and fall back to stdlib json (with tolerant UTF-8 decode) otherwise.
try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - depends on the environment
    _orjson = None


def _json_loads(raw: bytes):
    """Parse a JSON line from raw bytes, via orjson when present else stdlib json.

    The stdlib path keeps the original tolerant decode (`errors="replace"`) so a line
    with a stray bad byte but valid JSON still parses; orjson raises on invalid UTF-8,
    which `_ingest_line` already treats as malformed-and-skipped (never fatal, r4).
    """
    if _orjson is not None:
        return _orjson.loads(raw)
    return json.loads(raw.decode("utf-8", "replace"))


# Cheap byte prefilter for the two supported rollout schemas.
_MARK_USAGE = b'"usage"'
_MARK_ASSISTANT = b"assistant"
_MARK_TOKEN_COUNT = b'"token_count"'
_MARK_TURN_CONTEXT = b'"turn_context"'

# Keep file reads bounded even when a cold scan encounters a multi-gigabyte rollout.
# Individual JSONL records are still returned whole by readline(), but the file itself is
# never duplicated into one giant bytes object before parsing.
_READ_BUFFER_BYTES = 4 * 1024 * 1024

# Persistent-cache format version. Bump whenever the on-disk shape below or the
# extraction/cost logic changes, so an older cache is ignored rather than mis-read.
# v2: records gained the private cache_creation sub-buckets and the cache now stores
# a per-record dedup key so streaming merges (T9) survive a warm start.
# v4: Codex records seen before their first turn_context are retained as pending so a
# later model marker can reconcile them across incremental scans and warm starts.
# v5: records carry their account label (T11 multi-account) and the cache pins the
# active root set, so a changed root set rebuilds once instead of being mis-read.
_CACHE_VERSION = 5


@dataclass(slots=True)
class UsageRecord:
    ts: float  # epoch seconds (UTC)
    model_raw: str
    model_norm: str
    known: bool  # False -> model not in pricing (tokens kept; cost is unavailable)
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int  # aggregate creation tokens (5m + 1h)
    cost: float
    # Raw cache_creation sub-buckets, kept privately so a later streaming line for
    # the same message (T9) can be merged and its cost recomputed while preserving
    # the None-vs-0 distinction compute_cost relies on (None -> 1.25x aggregate
    # fallback). Not part of the public token/cost surface.
    _eph_5m: int | None = None
    _eph_1h: int | None = None
    # Account label the record belongs to (T11). A Claude root's label for Claude
    # records, `CODEX_ACCOUNT` for Codex. Defaults empty so records built without a
    # root (unit tests) round-trip harmlessly; the engine's scope filter and the
    # by-account rollup key on it. Kept last so positional cache tuples stay stable.
    account: str = ""

    @property
    def cache_tokens(self) -> int:
        return self.cache_read + self.cache_creation

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_creation


@dataclass
class ParseStats:
    files_seen: int = 0
    lines_read: int = 0
    records: int = 0  # unique usage records kept
    duplicates: int = 0  # repeat-key lines merged into an existing record (T9)
    malformed: int = 0
    unknown_models: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """One progress snapshot for transcript discovery or parsing."""

    phase: str
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_file: str | None = None


class ScanCancelled(RuntimeError):
    """The caller requested a resumable transcript-scan cancellation."""


ProgressCallback = Callable[[ScanProgress], None]
CancelCheck = Callable[[], bool]


@dataclass
class _FileState:
    offset: int = 0
    size: int = 0
    mtime: float = 0.0


def parse_timestamp(ts: object) -> float | None:
    """ISO-8601 (usually UTC '...Z') -> epoch seconds. None if unparseable."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # last-resort tolerant parse for a couple of common shapes
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(ts.replace("Z", "+0000"), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _legacy_default_roots() -> list[tuple[Path, str]]:
    """The pre-multi-account single Claude root: ``PROJECTS_DIR`` (+ the Codex
    active/archived dirs only when it is the canonical ``~/.claude/projects``).

    Read at call time so tests/embedders that monkeypatch ``PROJECTS_DIR`` scan
    only that explicit tree — and never pull in real Codex data — preserving the
    parser's long-standing hermetic override behavior. The engine passes explicit
    roots for genuine multi-account setups (see `Parser.__init__`).
    """
    roots: list[tuple[Path, str]] = [(PROJECTS_DIR, DEFAULT_LABEL)]
    if PROJECTS_DIR == Path.home() / ".claude" / "projects":
        roots.append((CODEX_SESSIONS_DIR, CODEX_ACCOUNT))
        roots.append((CODEX_ARCHIVED_SESSIONS_DIR, CODEX_ACCOUNT))
    return roots


def _dedup_key(obj: dict, msg: dict) -> tuple[object, object] | None:
    """(requestId, message.id). None when message.id is absent (can't dedup -> count)."""
    mid = msg.get("id")
    if not mid:
        return None
    return (obj.get("requestId"), mid)


def _max_opt(a: int | None, b: int | None) -> int | None:
    """Field-wise max that preserves the None ('sub-bucket absent') signal.

    compute_cost distinguishes None (no cache_creation object -> 1.25x fallback on
    the aggregate) from 0 (object present, bucket empty). When merging streaming
    lines keep None only if BOTH lacked the bucket; if either reported it, that
    value wins (real streaming lines all carry identical cache fields anyway)."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


class Parser:
    """Stateful, incremental transcript reader.

    Hold a per-message record index + per-file offsets across scans. The first scan
    reads everything; subsequent scans read only appended lines (M6), folding each
    into the record its (requestId, message.id) already points at (T9).
    """

    def __init__(
        self,
        pricing: dict[str, dict[str, float]],
        cache_path: Path | None = None,
        roots: list[tuple[Path, str]] | None = None,
    ):
        self.pricing = pricing
        # When set, scan() loads persisted state before its first read and save_cache()
        # can write it back. Left None (the default) the parser is fully self-contained
        # and touches no cache — which is what the unit tests rely on.
        self.cache_path = cache_path
        # Transcript roots to scan, each an (projects_dir, account_label) pair. The
        # engine passes explicit roots for multi-account setups; roots=None keeps the
        # legacy single-root behaviour driven by PROJECTS_DIR (hermetic under tests).
        self._roots: list[tuple[Path, str]] = (
            [(Path(p), str(label)) for p, label in roots]
            if roots is not None
            else _legacy_default_roots()
        )
        self._default_label = self._roots[0][1] if self._roots else DEFAULT_LABEL
        # path str -> account label; rebuilt on every discovery pass.
        self._account_by_path: dict[str, str] = {}
        self._cache_loaded = False
        self._cache_unvalidated = False
        self.records: list[UsageRecord] = []
        self.stats = ParseStats()
        # One kept UsageRecord per unique (requestId, message.id). A repeat key
        # doesn't drop the line — it merges into the record stored here (T9). The
        # values ARE the objects in self.records, so mutating in place is picked up
        # by aggregate()/series(). Persists across scans (and, via the cache, runs).
        self._by_key: dict[tuple[object, object], UsageRecord] = {}
        self._files: dict[str, _FileState] = {}
        # Codex token_count events carry the model in the preceding turn_context.
        self._file_models: dict[str, str] = {}
        # Some rollouts emit token_count before their first turn_context. Keep object
        # references so the first authoritative model marker can reattribute/reprice
        # those records instead of permanently inventing a literal `codex` model.
        self._codex_pending: dict[str, list[UsageRecord]] = {}
        self._codex_totals: dict[str, tuple[int, int, int]] = {}
        # Canonical local rate-limit capture, newest event across all Codex files.
        self.latest_rate_limits: dict | None = None


    # ── extraction ────────────────────────────────────────────────────────
    def _extract(self, obj: dict) -> UsageRecord | None:
        if obj.get("type") != "assistant":
            return None
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None

        model_raw = msg.get("model") or ""
        if model_raw == "<synthetic>":
            # Harness-generated, non-billable turn (0 tokens). Not real spend; skip
            # so it neither adds a noise row nor trips the unknown-model flag.
            return None

        def _int(v: object) -> int:
            return v if isinstance(v, int) else 0

        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))
        cache_creation_total = _int(usage.get("cache_creation_input_tokens"))

        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            eph_5m: int | None = _int(cc.get("ephemeral_5m_input_tokens"))
            eph_1h: int | None = _int(cc.get("ephemeral_1h_input_tokens"))
            # If the aggregate was absent but sub-buckets present, derive it.
            if cache_creation_total == 0 and (eph_5m or eph_1h):
                cache_creation_total = (eph_5m or 0) + (eph_1h or 0)
        else:
            eph_5m = eph_1h = None

        # Streaming merge (T9): Claude Code writes one transcript line per content
        # block of a streaming assistant reply, all sharing one (requestId,
        # message.id), and only output_tokens grows across them. Fold a repeat line
        # into the record we already kept — field-wise max of every counter
        # (monotonic within a message, so max is order-independent and robust to a
        # final line landing in a later scan than the first) — then recompute cost.
        # No timestamp is needed here: we keep the first line's ts so the record
        # never shifts time bucket as later lines arrive.
        key = _dedup_key(obj, msg)
        if key is not None:
            existing = self._by_key.get(key)
            if existing is not None:
                existing.input_tokens = max(existing.input_tokens, input_tokens)
                existing.output_tokens = max(existing.output_tokens, output_tokens)
                existing.cache_read = max(existing.cache_read, cache_read)
                existing.cache_creation = max(existing.cache_creation, cache_creation_total)
                existing._eph_5m = _max_opt(existing._eph_5m, eph_5m)
                existing._eph_1h = _max_opt(existing._eph_1h, eph_1h)
                existing.cost = compute_cost(
                    input_tokens=existing.input_tokens,
                    output_tokens=existing.output_tokens,
                    cache_read=existing.cache_read,
                    cache_creation_total=existing.cache_creation,
                    ephemeral_5m=existing._eph_5m,
                    ephemeral_1h=existing._eph_1h,
                    rates=get_rates(existing.model_raw, self.pricing),
                )
                self.stats.duplicates += 1
                return None  # merged in place; no new record to append

        ts = parse_timestamp(obj.get("timestamp"))
        if ts is None:
            # No usable timestamp -> can't window it; skip rather than guess.
            return None

        model_norm = normalize_model(model_raw)
        rates = get_rates(model_raw, self.pricing)
        known = rates is not None
        if not known and model_norm:
            self.stats.unknown_models.add(model_norm)

        cost = compute_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation_total=cache_creation_total,
            ephemeral_5m=eph_5m,
            ephemeral_1h=eph_1h,
            rates=rates,
        )
        rec = UsageRecord(
            ts=ts,
            model_raw=model_raw,
            model_norm=model_norm or "(unknown)",
            known=known,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation_total,
            cost=cost,
            _eph_5m=eph_5m,
            _eph_1h=eph_1h,
        )
        # Register only once a real record exists, so a first line that lacks a
        # timestamp doesn't claim the key and strand the message's later lines.
        if key is not None:
            self._by_key[key] = rec
        return rec

    @staticmethod
    def _codex_int(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    def _capture_codex_limits(self, payload: dict, ts: float) -> None:
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            return
        buckets: dict[str, dict[str, float]] = {}
        for name in ("primary", "secondary"):
            value = limits.get(name)
            if not isinstance(value, dict):
                continue
            used = value.get("used_percent")
            resets = value.get("resets_at")
            if not isinstance(used, (int, float)) or not isinstance(resets, (int, float)):
                continue
            bucket: dict[str, float] = {
                "used_percentage": float(used),
                "resets_at": float(resets),
            }
            minutes = value.get("window_minutes")
            if isinstance(minutes, (int, float)):
                bucket["window_minutes"] = float(minutes)
            buckets[f"codex_{name}"] = bucket
        if not buckets:
            return
        capture = {"captured_at": ts, "source": "codex", "rate_limits": buckets}
        current_ts = (self.latest_rate_limits or {}).get("captured_at", float("-inf"))
        if not isinstance(current_ts, (int, float)) or ts >= current_ts:
            self.latest_rate_limits = capture

    def _reconcile_codex_model(self, source: str, model: str) -> None:
        """Make a turn_context authoritative for pre-context records in one rollout."""
        self._file_models[source] = model
        pending = self._codex_pending.pop(source, [])
        if not pending:
            return
        model_norm = normalize_model(model) or "codex-unattributed"
        rates = get_rates(model, self.pricing)
        known = rates is not None
        for record in pending:
            record.model_raw = model
            record.model_norm = model_norm
            record.known = known
            record.cost = compute_cost(
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_read=record.cache_read,
                cache_creation_total=record.cache_creation,
                ephemeral_5m=record._eph_5m,
                ephemeral_1h=record._eph_1h,
                rates=rates,
            )
        # Rebuild rather than discard one name blindly: another file may still contain
        # genuinely unattributed records, and the authoritative model may itself be
        # unpriced (for example codex-auto-review).
        self.stats.unknown_models = {
            record.model_norm
            for record in self.records
            if not record.known and record.model_norm
        }

    def _extract_codex(self, obj: dict, source: str) -> UsageRecord | None:
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        ts = parse_timestamp(obj.get("timestamp"))
        if ts is None:
            return None
        self._capture_codex_limits(payload, ts)
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        total = info.get("total_token_usage")
        last = info.get("last_token_usage")
        current_total = None
        if isinstance(total, dict):
            current_total = (
                self._codex_int(total.get("input_tokens")),
                self._codex_int(total.get("cached_input_tokens")),
                self._codex_int(total.get("output_tokens")),
            )
        if isinstance(last, dict):
            raw_input = self._codex_int(last.get("input_tokens"))
            cache_read = min(raw_input, self._codex_int(last.get("cached_input_tokens")))
            output_tokens = self._codex_int(last.get("output_tokens"))
        elif current_total is not None:
            previous = self._codex_totals.get(source, (0, 0, 0))
            raw_input = max(0, current_total[0] - previous[0])
            cache_read = min(raw_input, max(0, current_total[1] - previous[1]))
            output_tokens = max(0, current_total[2] - previous[2])
        else:
            return None
        if current_total is not None:
            self._codex_totals[source] = current_total
        if raw_input == 0 and output_tokens == 0:
            return None

        # Codex input_tokens includes cached_input_tokens; keep cached input separate.
        input_tokens = raw_input - cache_read
        model_raw = self._file_models.get(source, "codex-unattributed")
        model_norm = normalize_model(model_raw) or "codex-unattributed"
        rates = get_rates(model_raw, self.pricing)
        known = rates is not None
        if not known:
            self.stats.unknown_models.add(model_norm)
        cost = compute_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation_total=0,
            ephemeral_5m=0,
            ephemeral_1h=0,
            rates=rates,
        )
        return UsageRecord(
            ts=ts,
            model_raw=model_raw,
            model_norm=model_norm,
            known=known,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=0,
            cost=cost,
            _eph_5m=0,
            _eph_1h=0,
        )

    def _ingest_line(
        self, raw: bytes, source: str = "", account_label: str | None = None
    ) -> None:
        is_claude = _MARK_USAGE in raw and _MARK_ASSISTANT in raw
        is_codex = _MARK_TOKEN_COUNT in raw or _MARK_TURN_CONTEXT in raw
        if not is_claude and not is_codex:
            return
        try:
            obj = _json_loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.stats.malformed += 1
            return
        if not isinstance(obj, dict):
            return
        if obj.get("type") == "turn_context":
            payload = obj.get("payload")
            model = payload.get("model") if isinstance(payload, dict) else None
            if isinstance(model, str) and model:
                self._reconcile_codex_model(source, model)
            return
        self.stats.lines_read += 1
        rec = self._extract_codex(obj, source) if is_codex else self._extract(obj)
        if rec is not None:
            # Codex records are always the Codex account regardless of where the file
            # sits; Claude records take their root's label (looked up from the path
            # when the caller didn't supply one, e.g. ingest_file / a bare _read_new).
            if account_label is None:
                account_label = self._account_by_path.get(source, self._default_label)
            rec.account = CODEX_ACCOUNT if is_codex else account_label
            self.records.append(rec)
            if is_codex and source not in self._file_models:
                self._codex_pending.setdefault(source, []).append(rec)
            self.stats.records += 1

    # ── incremental file reading ──────────────────────────────────────────
    def _read_new(
        self,
        path: Path,
        *,
        on_bytes: Callable[[int], None] | None = None,
        cancelled: CancelCheck | None = None,
    ) -> None:
        sp = str(path)
        account = self._account_by_path.get(sp, self._default_label)
        try:
            st = path.stat()
        except OSError:
            return
        state = self._files.get(sp)
        if state is None:
            state = _FileState()
            self._files[sp] = state
        elif st.st_size == state.size and st.st_mtime == state.mtime:
            return

        start = state.offset
        if st.st_size < start:
            start = 0

        consumed = 0
        try:
            with open(path, "rb", buffering=_READ_BUFFER_BYTES) as fh:
                fh.seek(start)
                while raw_line := fh.readline():
                    if cancelled is not None and cancelled():
                        # Persist the last ingested line boundary so retry resumes safely.
                        state.offset = start + consumed
                        raise ScanCancelled("transcript scan cancelled")
                    if not raw_line.endswith(b"\n"):
                        # Leave an incomplete tail before the offset. When it is appended
                        # to, the size/mtime change below makes the next scan revisit it.
                        if on_bytes is not None:
                            on_bytes(len(raw_line))
                        break
                    line = raw_line[:-1]
                    if line.strip():
                        self._ingest_line(line, sp, account)
                    consumed += len(raw_line)
                    if on_bytes is not None:
                        on_bytes(len(raw_line))
        except ScanCancelled:
            raise
        except OSError:
            # A later scan resumes after the last complete line already ingested rather
            # than repeating non-deduplicated Codex token-count events.
            state.offset = start + consumed
            return

        state.offset = start + consumed
        state.size = st.st_size
        state.mtime = st.st_mtime

    def _discover_files(self, cancelled: CancelCheck | None = None) -> list[Path]:
        """All transcript files across the configured roots, sorted, tagged by account.

        Claude project trees are recursive (subagent/workflow usage lives below the
        session root); Codex active + archived rollouts are flat siblings. All roots
        are read-only. Rebuilds `self._account_by_path` so each file's records can be
        tagged with its root's label; the first root to claim a path wins (roots are
        already deduplicated by the engine, so this only guards pathological overlap).
        """
        tagged: dict[Path, str] = {}
        for projects_dir, label in self._roots:
            if not projects_dir.is_dir():
                continue
            try:
                for path in projects_dir.rglob("*.jsonl"):
                    if cancelled is not None and cancelled():
                        raise ScanCancelled("transcript discovery cancelled")
                    tagged.setdefault(path, label)
            except OSError:
                continue
        ordered = sorted(tagged)
        self._account_by_path = {str(p): tagged[p] for p in ordered}
        return ordered

    def scan(
        self,
        progress: ProgressCallback | None = None,
        cancelled: CancelCheck | None = None,
    ) -> ParseStats:
        """Reconcile cached paths, then read new bytes with progress/cancellation."""
        if progress is not None:
            progress(ScanProgress(phase="discovering"))
        files = self._discover_files(cancelled)
        if cancelled is not None and cancelled():
            if progress is not None:
                progress(ScanProgress(phase="cancelled", files_total=len(files)))
            raise ScanCancelled("transcript scan cancelled")

        current_paths = {str(p) for p in files}
        if self.cache_path is not None and not self._cache_loaded:
            self._cache_loaded = True
            self._load_cache(current_paths)
        elif self._cache_unvalidated:
            if not self._reconcile_cached_paths(current_paths):
                self._clear_cache_state()
            self._cache_unvalidated = False
        self.stats.files_seen = len(files)

        pending_bytes: dict[Path, int] = {}
        for path in files:
            try:
                st = path.stat()
            except OSError:
                pending_bytes[path] = 0
                continue
            state = self._files.get(str(path))
            if state is not None and st.st_size == state.size and st.st_mtime == state.mtime:
                pending_bytes[path] = 0
            else:
                start = state.offset if state is not None else 0
                pending_bytes[path] = st.st_size if st.st_size < start else st.st_size - start

        bytes_total = sum(pending_bytes.values())
        bytes_done = 0

        def emit(phase: str, files_done: int, current_file: str | None = None) -> None:
            if progress is not None:
                progress(
                    ScanProgress(
                        phase=phase,
                        files_done=files_done,
                        files_total=len(files),
                        bytes_done=bytes_done,
                        bytes_total=max(bytes_total, bytes_done),
                        current_file=current_file,
                    )
                )

        emit("parsing", 0)
        for index, path in enumerate(files):
            if cancelled is not None and cancelled():
                emit("cancelled", index)
                raise ScanCancelled("transcript scan cancelled")

            def advance(count: int, *, _path: Path = path, _index: int = index) -> None:
                nonlocal bytes_done
                bytes_done += count
                emit("parsing", _index, _path.name)

            self._read_new(path, on_bytes=advance, cancelled=cancelled)
            emit("parsing", index + 1, path.name)

        emit("complete", len(files))
        return self.stats
    def prime_cache(self) -> bool:
        """Load cached aggregates immediately; filesystem reconciliation can follow."""
        if self.cache_path is None:
            return False
        if self._cache_loaded:
            return bool(self.records)
        self._cache_loaded = True
        loaded = self._load_cache(None)
        self._cache_unvalidated = loaded
        return loaded

    def _clear_cache_state(self) -> None:
        self.records = []
        self.stats = ParseStats()
        self._by_key = {}
        self._files = {}
        self._file_models = {}
        self._codex_pending = {}
        self._codex_totals = {}
        self.latest_rate_limits = None
        self._cache_unvalidated = False

    def _reconcile_cached_paths(self, current_paths: set[str]) -> bool:
        """Accept Codex active→archive moves without invalidating the whole cache."""
        missing = [path for path in self._files if path not in current_paths]
        if not missing:
            return True
        by_name: dict[str, list[str]] = {}
        for path in current_paths - set(self._files):
            by_name.setdefault(Path(path).name, []).append(path)
        remaps: dict[str, str] = {}
        for old in missing:
            name = Path(old).name
            candidates = by_name.get(name, [])
            if not name.startswith("rollout-") or len(candidates) != 1:
                return False
            new = candidates[0]
            try:
                if Path(new).stat().st_size < self._files[old].offset:
                    return False
            except OSError:
                return False
            remaps[old] = new
        for old, new in remaps.items():
            self._files[new] = self._files.pop(old)
            if old in self._file_models:
                self._file_models[new] = self._file_models.pop(old)
            if old in self._codex_pending:
                self._codex_pending[new] = self._codex_pending.pop(old)
            if old in self._codex_totals:
                self._codex_totals[new] = self._codex_totals.pop(old)
        return True

    # ── persistent cache (M6 across process runs) ─────────────────────────────
    def _pricing_fingerprint(self) -> str:
        """Stable hash of the active pricing table.

        Cached records carry a *computed* cost, so a cache built under different rates
        must be discarded — this fingerprint, stored in the cache, detects that.
        """
        blob = json.dumps(self.pricing, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _roots_fingerprint(self) -> str:
        """Stable hash of the active root set (T11).

        Cached records carry an account label tied to a specific set of roots, so a
        changed root set (a root added, removed, enabled or disabled) must rebuild the
        cache once rather than serve stale/mislabeled records. Order-independent."""
        blob = json.dumps(
            sorted([str(p), label] for p, label in self._roots), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _load_cache(self, current_paths: set[str] | None) -> bool:
        """Load a validated cache; optionally reconcile it with current paths."""
        try:
            with open(self.cache_path, "rb") as fh:
                data = pickle.load(fh)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("version") != _CACHE_VERSION:
            return False
        if data.get("pricing_fp") != self._pricing_fingerprint():
            return False
        if data.get("roots_fp") != self._roots_fingerprint():
            return False
        files = data.get("files")
        records = data.get("records")
        keys = data.get("keys")
        codex = data.get("codex")
        if (
            not isinstance(files, dict)
            or records is None
            or keys is None
            or not isinstance(codex, dict)
        ):
            return False
        try:
            recs = [UsageRecord(*item) for item in records]
            by_key: dict[tuple[object, object], UsageRecord] = {}
            for rec, key in zip(recs, keys, strict=True):
                if key is not None:
                    by_key[tuple(key)] = rec
            self.records = recs
            self._by_key = by_key
            self._files = {
                path: _FileState(offset=offset, size=size, mtime=mtime)
                for path, (offset, size, mtime) in files.items()
            }
            file_models = codex.get("file_models", {})
            pending = codex.get("pending", {})
            totals = codex.get("totals", {})
            latest = codex.get("latest_rate_limits")
            if (
                not isinstance(file_models, dict)
                or not isinstance(pending, dict)
                or not isinstance(totals, dict)
            ):
                raise TypeError("invalid Codex cache state")
            self._file_models = {str(key): str(value) for key, value in file_models.items()}
            self._codex_pending = {
                str(source): [self.records[int(index)] for index in indices]
                for source, indices in pending.items()
            }
            self._codex_totals = {str(key): tuple(value) for key, value in totals.items()}
            self.latest_rate_limits = latest if isinstance(latest, dict) else None
        except (IndexError, KeyError, OverflowError, TypeError, ValueError):
            self._clear_cache_state()
            return False
        self.stats.records = len(self.records)
        self.stats.unknown_models = {
            record.model_norm
            for record in self.records
            if not record.known and record.model_norm
        }
        if current_paths is not None and not self._reconcile_cached_paths(current_paths):
            self._clear_cache_state()
            return False
        return True
    def save_cache(self) -> None:
        """Persist current state to the cache path (atomic; no-op if unconfigured).

        Serialises records as plain tuples (not the dataclass) so the on-disk format
        is decoupled from the class layout, and writes via a temp file + os.replace so
        a crash mid-write can never leave a half-written cache. Any write error is
        swallowed — the cache is an optimisation, never load-bearing."""
        if self.cache_path is None:
            return
        # Reverse index: the dedup key (if any) that points at each record, so a
        # warm start can rebuild self._by_key with the same object identity (T9).
        rec_key = {id(r): k for k, r in self._by_key.items()}
        rec_index = {id(r): index for index, r in enumerate(self.records)}
        data = {
            "version": _CACHE_VERSION,
            "pricing_fp": self._pricing_fingerprint(),
            "roots_fp": self._roots_fingerprint(),
            "files": {
                p: (s.offset, s.size, s.mtime) for p, s in self._files.items()
            },
            "records": [
                (
                    r.ts,
                    r.model_raw,
                    r.model_norm,
                    r.known,
                    r.input_tokens,
                    r.output_tokens,
                    r.cache_read,
                    r.cache_creation,
                    r.cost,
                    r._eph_5m,
                    r._eph_1h,
                    r.account,
                )
                for r in self.records
            ],
            "keys": [rec_key.get(id(r)) for r in self.records],
            "codex": {
                "file_models": self._file_models,
                "pending": {
                    source: [rec_index[id(record)] for record in records]
                    for source, records in self._codex_pending.items()
                },
                "totals": self._codex_totals,
                "latest_rate_limits": self.latest_rate_limits,
            },
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.cache_path)
        except OSError:
            return

    def ingest_file(self, path: str | Path) -> None:
        """Read one file in full (non-incremental). Used by tests and one-shots."""
        try:
            with open(path, "rb") as fh:
                for line in fh:
                    if line.strip():
                        self._ingest_line(line, str(path))
        except OSError:
            return
