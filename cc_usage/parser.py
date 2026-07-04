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

from .cost import compute_cost, get_rates, normalize_model
from .paths import PROJECTS_DIR

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


# Cheap byte prefilter: an assistant usage line must contain both markers. No false
# negatives (an assistant record with usage always has them), so this only skips
# lines we'd discard anyway — it just avoids a full json.loads on most lines.
_MARK_USAGE = b'"usage"'
_MARK_ASSISTANT = b"assistant"

# Persistent-cache format version. Bump whenever the on-disk shape below or the
# extraction/cost logic changes, so an older cache is ignored rather than mis-read.
# v2: records gained the private cache_creation sub-buckets and the cache now stores
# a per-record dedup key so streaming merges (T9) survive a warm start.
_CACHE_VERSION = 2


@dataclass(slots=True)
class UsageRecord:
    ts: float  # epoch seconds (UTC)
    model_raw: str
    model_norm: str
    known: bool  # False -> model not in pricing (cost contributed 0)
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


def iter_transcript_files() -> list[Path]:
    """All transcript files across every project dir (sorted, deterministic).

    Recursive: subagent/workflow transcripts live deeper, under
    `<session>/subagents/workflows/wf_*/...`. Their usage is real spend (T0 §4A),
    so we must descend into them — a one-level glob misses ~90% of the data.
    Global dedup by (requestId, message.id) prevents double-counting lines that a
    parent session and a subagent file both contain.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    try:
        return sorted(PROJECTS_DIR.rglob("*.jsonl"))
    except OSError:
        return []


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
    ):
        self.pricing = pricing
        # When set, scan() loads persisted state before its first read and save_cache()
        # can write it back. Left None (the default) the parser is fully self-contained
        # and touches no cache — which is what the unit tests rely on.
        self.cache_path = cache_path
        self._cache_loaded = False
        self.records: list[UsageRecord] = []
        self.stats = ParseStats()
        # One kept UsageRecord per unique (requestId, message.id). A repeat key
        # doesn't drop the line — it merges into the record stored here (T9). The
        # values ARE the objects in self.records, so mutating in place is picked up
        # by aggregate()/series(). Persists across scans (and, via the cache, runs).
        self._by_key: dict[tuple[object, object], UsageRecord] = {}
        self._files: dict[str, _FileState] = {}

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

    def _ingest_line(self, raw: bytes) -> None:
        if _MARK_USAGE not in raw or _MARK_ASSISTANT not in raw:
            return
        self.stats.lines_read += 1
        try:
            obj = _json_loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.stats.malformed += 1
            return
        if not isinstance(obj, dict):
            return
        rec = self._extract(obj)
        if rec is not None:
            self.records.append(rec)
            self.stats.records += 1

    # ── incremental file reading ──────────────────────────────────────────
    def _read_new(self, path: Path) -> None:
        sp = str(path)
        try:
            st = path.stat()
        except OSError:
            return
        state = self._files.get(sp)
        if state is None:
            state = _FileState()
            self._files[sp] = state
        elif st.st_size == state.size and st.st_mtime == state.mtime:
            return  # unchanged since last scan -> skip entirely (the M6 win)

        start = state.offset
        if st.st_size < start:
            start = 0  # truncated/rotated -> re-read from the top

        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            return

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet (mid-append); leave offset, just refresh stat.
            state.size, state.mtime = st.st_size, st.st_mtime
            return
        complete = chunk[: last_nl + 1]
        for line in complete.split(b"\n"):
            if line.strip():
                self._ingest_line(line)

        state.offset = start + last_nl + 1
        state.size = st.st_size
        state.mtime = st.st_mtime

    def scan(self) -> ParseStats:
        """Read any new transcript lines and fold them into running aggregates.

        On the *first* scan, if a cache path is configured, previously persisted
        state (records + dedup set + per-file offsets) is loaded first, so only
        transcript bytes appended since that snapshot are read now (M6 across runs).
        """
        files = iter_transcript_files()
        if self.cache_path is not None and not self._cache_loaded:
            self._cache_loaded = True
            self._load_cache({str(p) for p in files})
        self.stats.files_seen = len(files)
        for path in files:
            self._read_new(path)
        return self.stats

    # ── persistent cache (M6 across process runs) ─────────────────────────────
    def _pricing_fingerprint(self) -> str:
        """Stable hash of the active pricing table.

        Cached records carry a *computed* cost, so a cache built under different rates
        must be discarded — this fingerprint, stored in the cache, detects that.
        """
        blob = json.dumps(self.pricing, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _load_cache(self, current_paths: set[str]) -> None:
        """Populate state from the on-disk cache, or leave it empty on any mismatch.

        The cache is honoured only when it is fully consistent with *this* run:
        matching format version, matching pricing fingerprint, and every cached file
        still present on disk. That last check matters — records don't carry their
        source file, so a *deleted* transcript couldn't be subtracted from the flat
        record list; discarding the cache and rescanning keeps a warm start's totals
        byte-identical to a cold one. ANY error (missing/corrupt/incompatible pickle)
        degrades silently to an empty state → a normal full scan (Rulebook r4).
        """
        try:
            with open(self.cache_path, "rb") as fh:
                data = pickle.load(fh)
        except Exception:
            # Deliberately broad: unpickling malformed input can raise almost anything
            # (the pickle docs say as much). A bad cache must never be fatal — it just
            # means a cold scan this time (Rulebook r4). The data is re-validated below.
            return
        if not isinstance(data, dict):
            return
        if data.get("version") != _CACHE_VERSION:
            return
        if data.get("pricing_fp") != self._pricing_fingerprint():
            return
        files = data.get("files")
        records = data.get("records")
        keys = data.get("keys")
        if not isinstance(files, dict) or records is None or keys is None:
            return
        # A cached file that no longer exists would leave orphaned records we can't
        # remove → discard the whole cache and let scan() rebuild from disk.
        if any(p not in current_paths for p in files):
            return
        try:
            recs = [UsageRecord(*t) for t in records]
            # Rebuild the per-message index from the parallel key list, pointing at
            # the SAME record objects so a streaming line appended after this warm
            # start merges into the cached record in place (T9).
            by_key: dict[tuple[object, object], UsageRecord] = {}
            for rec, k in zip(recs, keys, strict=True):
                if k is not None:
                    by_key[tuple(k)] = rec
            self.records = recs
            self._by_key = by_key
            self._files = {
                p: _FileState(offset=o, size=s, mtime=m)
                for p, (o, s, m) in files.items()
            }
        except (TypeError, ValueError):
            # A shape we don't recognise — treat exactly like a cold start.
            self.records = []
            self._by_key = {}
            self._files = {}
            return
        self.stats.records = len(self.records)
        self.stats.unknown_models = {
            r.model_norm for r in self.records if not r.known and r.model_norm
        }

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
        data = {
            "version": _CACHE_VERSION,
            "pricing_fp": self._pricing_fingerprint(),
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
                )
                for r in self.records
            ],
            "keys": [rec_key.get(id(r)) for r in self.records],
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
                        self._ingest_line(line)
        except OSError:
            return
