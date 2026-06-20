"""Transcript parser (T0 §4A) — dedup, tolerant extraction, incremental reads.

Scans ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl. For each *assistant*
record that carries a usage object it emits one deduplicated UsageRecord, keyed by
(requestId, message.id) so retries/sidechain echoes are counted once. Sidechain /
subagent usage is included (it is real spend). Anything malformed or non-usage is
skipped, never fatal (Rulebook rule 4).

Reads are incremental (M6 / T0 §9): each file's byte offset + size + mtime are
remembered, and on a later scan only newly appended *complete* lines are read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .cost import compute_cost, get_rates, normalize_model
from .paths import PROJECTS_DIR

# Cheap byte prefilter: an assistant usage line must contain both markers. No false
# negatives (an assistant record with usage always has them), so this only skips
# lines we'd discard anyway — it just avoids a full json.loads on most lines.
_MARK_USAGE = b'"usage"'
_MARK_ASSISTANT = b"assistant"


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
    duplicates: int = 0
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


class Parser:
    """Stateful, incremental transcript reader.

    Hold a dedup set + per-file offsets across scans. The first scan reads
    everything; subsequent scans read only appended lines (M6).
    """

    def __init__(self, pricing: dict[str, dict[str, float]]):
        self.pricing = pricing
        self.records: list[UsageRecord] = []
        self.stats = ParseStats()
        self._seen: set[tuple[object, object]] = set()
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

        key = _dedup_key(obj, msg)
        if key is not None:
            if key in self._seen:
                self.stats.duplicates += 1
                return None
            self._seen.add(key)

        ts = parse_timestamp(obj.get("timestamp"))
        if ts is None:
            # No usable timestamp -> can't window it; skip rather than guess.
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
        return UsageRecord(
            ts=ts,
            model_raw=model_raw,
            model_norm=model_norm or "(unknown)",
            known=known,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation_total,
            cost=cost,
        )

    def _ingest_line(self, raw: bytes) -> None:
        if _MARK_USAGE not in raw or _MARK_ASSISTANT not in raw:
            return
        self.stats.lines_read += 1
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
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
        """Read any new transcript lines and fold them into running aggregates."""
        files = iter_transcript_files()
        self.stats.files_seen = len(files)
        for path in files:
            self._read_new(path)
        return self.stats

    def ingest_file(self, path: str | Path) -> None:
        """Read one file in full (non-incremental). Used by tests and one-shots."""
        try:
            with open(path, "rb") as fh:
                for line in fh:
                    if line.strip():
                        self._ingest_line(line)
        except OSError:
            return
