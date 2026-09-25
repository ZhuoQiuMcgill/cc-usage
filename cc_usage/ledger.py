"""Durable, content-free usage ledger (T17).

Claude Code deletes old transcripts (30 days by default), and ccusage's history used to
live only in those transcripts: once one was gone, a rebuilt parse cache simply no
longer had its usage. The ledger is ccusage's own record of every usage event it has
ever parsed, kept in ``~/.config/cc-usage/ledger.sqlite3``. With it, a record that was
seen once stays in every view after its transcript is deleted.

What a row holds — tokens only, never content:

* ``key``    the event's stable 64-bit key (``UsageRecord.lkey``); INTEGER PRIMARY KEY,
             so the key *is* the rowid and the table needs no second index;
* ``acct``   the account (a root's identity + provider + last label), interned;
* ``ts``     integer epoch milliseconds;
* ``model``  the raw model id, interned;
* ``inp``/``outp``/``cr``/``cc``  input, output, cache-read and cache-creation tokens;
* ``e5``/``e1``  the ephemeral 5m / 1h cache-creation sub-buckets, NULL when the
             transcript had no sub-bucket object (``compute_cost`` prices NULL and 0
             differently, so the distinction is kept).

No cost is stored: it is recomputed from the current pricing table whenever a row is
loaded, so a pricing fix applies to all of history. No prompt, response, tool I/O, file
path, cwd, project name or branch is ever written; the account identity is a digest of
the root's resolved path, not the path.

Writes are one short ``BEGIN IMMEDIATE`` transaction per sync, merged per key by the
parser's own rule — field-wise max (T9) — and a ``codex-unattributed`` model is replaced
once the rollout's turn_context resolves it. WAL mode plus a busy timeout let several
ccusage processes share the file. Every failure surfaces as a `LedgerError` subclass so
the engine can degrade to a panel warning and keep running (T14).

**Compatibility rule — read before changing the parser.** Once transcripts are deleted
the ledger is the only copy of their usage, and it was written by *older* parser code.
The engine treats "stored but not parsed" as history and merges by field-wise max, so a
parser change that does any of the following silently corrupts history unless it ships
with a ledger migration:

* **changes how a key is derived** (every old row would come back as an orphan and be
  counted a second time next to its re-keyed live twin);
* **stops emitting a record it used to emit** (the old row comes back as an orphan);
* **lowers a record's values** (the old, higher row wins the max merge forever).

Such a change must bump `KEY_SCHEME` (in parser.py, next to the key derivation) *and*
register a function in `KEY_SCHEME_MIGRATIONS` that rewrites the stored rows to the new
rules — delete, re-key or lower them — inside the transaction the ledger opens for it.
Bump the parse cache's `_CACHE_VERSION` too. A ledger whose scheme has no migration path
is refused (the panel warns and runs without it) rather than double counted; a ledger
written by a *newer* scheme is left untouched.

Backups and recovery. Once a day the worker writes ``ledger.sqlite3.bak`` with SQLite's
online backup API, verifies it with ``quick_check`` and rotates the previous one to
``ledger.sqlite3.bak.prev``. Every ledger has a random ``ledger_id`` (its *lineage*,
copied into its backups) and records in ``meta.merged`` the lineages whose rows it has
fully absorbed. A backup slot is only ever replaced when the file in it is *covered* —
its lineage is this ledger's own or one it merged — so the only good copy of some
history is never overwritten.

Recovery is a queue (``meta.pending``) that survives restarts. An unreadable ledger is
renamed aside (never deleted) and queued, together with the backups; so is a backup
found to be uncovered, and so are the backups when a ledger has to be created while
they exist (the ledger file went missing). Each queued file is read from a scratch copy
next to the ledger (never TMPDIR), salvaged by key range around damaged pages, checked
with ``quick_check``, migrated to the current key scheme, and merged by the same max
rule. A file that cannot be read for an environmental reason (disk full, permissions)
stays queued and is retried; while anything is queued no backup is rotated. A file
whose key scheme is unknown or cannot be migrated is refused and kept.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .accounts import CODEX_PROVIDER
from .cost import compute_cost, get_rates, normalize_model
from .parser import KEY_SCHEME, UsageRecord

# v1: the pre-release T17 build (no meta table). v2 adds `meta` (key scheme, lineage).
SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
# Keep the WAL from lingering at the size of the first (backfill) transaction.
_JOURNAL_SIZE_LIMIT = 4 * 1024 * 1024
UNATTRIBUTED = "codex-unattributed"
BACKUP_INTERVAL_SECS = 24 * 3600
# Batch size for `key IN (...)` lookups: well under SQLite's variable limit on every
# supported version (999 before 3.32).
_IN_BATCH = 500
# Past this many wanted rows one sequential pass beats hundreds of batched lookups.
_SCAN_ALL_OVER = 20_000

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        identity TEXT NOT NULL,
        label TEXT NOT NULL,
        UNIQUE (provider, identity)
    )""",
    "CREATE TABLE IF NOT EXISTS models (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    """CREATE TABLE IF NOT EXISTS usage (
        key INTEGER PRIMARY KEY,
        acct INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        model INTEGER NOT NULL,
        inp INTEGER NOT NULL,
        outp INTEGER NOT NULL,
        cr INTEGER NOT NULL,
        cc INTEGER NOT NULL,
        e5 INTEGER,
        e1 INTEGER
    )""",
)
_META = "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID"

# Merge an incoming row into a stored one exactly as the parser merges streaming lines
# (field-wise max, NULL sub-bucket kept only while both sides lack it), and let a
# resolved model replace `codex-unattributed`. The WHERE clause skips no-op updates so a
# re-sent unchanged row writes no page. `{u}` is the interned id of codex-unattributed.
_UPSERT = """
INSERT INTO usage (key, acct, ts, model, inp, outp, cr, cc, e5, e1)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (key) DO UPDATE SET
    inp = max(usage.inp, excluded.inp),
    outp = max(usage.outp, excluded.outp),
    cr = max(usage.cr, excluded.cr),
    cc = max(usage.cc, excluded.cc),
    e5 = CASE WHEN usage.e5 IS NULL THEN excluded.e5
              WHEN excluded.e5 IS NULL THEN usage.e5
              ELSE max(usage.e5, excluded.e5) END,
    e1 = CASE WHEN usage.e1 IS NULL THEN excluded.e1
              WHEN excluded.e1 IS NULL THEN usage.e1
              ELSE max(usage.e1, excluded.e1) END,
    model = CASE WHEN usage.model = {u} AND excluded.model != {u}
                 THEN excluded.model ELSE usage.model END
WHERE excluded.inp > usage.inp
   OR excluded.outp > usage.outp
   OR excluded.cr > usage.cr
   OR excluded.cc > usage.cc
   OR (excluded.e5 IS NOT NULL AND (usage.e5 IS NULL OR excluded.e5 > usage.e5))
   OR (excluded.e1 IS NOT NULL AND (usage.e1 IS NULL OR excluded.e1 > usage.e1))
   OR (usage.model = {u} AND excluded.model != {u})
"""

_ROW_COLUMNS = "key, acct, ts, model, inp, outp, cr, cc, e5, e1"


# ── key-scheme migrations (see the compatibility rule above) ─────────────────────
def _migrate_scheme_0(conn: sqlite3.Connection) -> None:
    """Scheme 0 -> 1: the pre-release T17 build keyed Codex events with an ordinal and
    stored Codex re-emissions as usage. Neither can be re-keyed from a stored row, so
    its Codex rows are dropped, and the next full sync re-records only the Codex usage
    whose rollouts are still on disk (scheme 0 never shipped, so no released ledger has
    Codex history that exists nowhere else). Claude keys did not change and are kept."""
    conn.execute(
        "DELETE FROM usage WHERE acct IN (SELECT id FROM accounts WHERE provider = ?)",
        (CODEX_PROVIDER,),
    )


# KEY_SCHEME_MIGRATIONS[n] upgrades a ledger written under key scheme n to n + 1. Each
# runs inside the write transaction that also records the new scheme, so a migration is
# all-or-nothing and runs exactly once per ledger.
KEY_SCHEME_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _migrate_scheme_0,
}


class LedgerError(Exception):
    """The ledger could not be used for this operation (never fatal to the panel)."""


class LedgerBusy(LedgerError):
    """Another process held the database past the busy timeout; retry next scan."""


class LedgerCorrupt(LedgerError):
    """The file is not a readable SQLite database (it gets moved aside, not deleted)."""


class LedgerUnavailable(LedgerError):
    """Disk full, read-only or unopenable location, or an incompatible ledger: run
    without it."""


class RecoveryFailed(LedgerError):
    """A recovery source could not be read for an environmental reason (disk full,
    permissions, I/O). It stays queued and is retried; nothing may treat it as merged."""


class SourceRefused(LedgerError):
    """A recovery source's rows cannot be merged safely (its record key scheme is
    unknown, newer, or has no migration). The file is kept untouched."""


_SQLITE_HEADER = b"SQLite format 3\x00"
# SQLite primary result codes (the low byte of an extended code).
_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_CORRUPT, _SQLITE_NOTADB = 5, 6, 11, 26


def _classify(exc: BaseException) -> LedgerError:
    """Map a sqlite3/OS error onto the three ways the engine reacts to a failure.

    Uses the error code where Python exposes it (3.11+) and the message otherwise."""
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    if primary in (_SQLITE_CORRUPT, _SQLITE_NOTADB) or (
        primary is None and ("malformed" in lowered or "not a database" in lowered)
    ):
        return LedgerCorrupt(message)
    if primary in (_SQLITE_BUSY, _SQLITE_LOCKED) or (
        primary is None and ("locked" in lowered or "busy" in lowered)
    ):
        return LedgerBusy(message)
    return LedgerUnavailable(message)


def _file_id(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _text(value: str) -> str:
    """A str SQLite can store. Transcripts are JSON, and a JSON-escaped lone surrogate
    (``"\\ud800"``) survives `json.loads` as a str that UTF-8 cannot encode; binding it
    would fail the whole write. Such a character becomes U+FFFD in the stored copy."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return value


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
        (key, value),
    )


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One usage record as the ledger stores it (account/model not yet interned)."""

    key: int
    provider: str
    identity: str
    label: str
    ts_ms: int
    model: str
    inp: int
    outp: int
    cr: int
    cc: int
    e5: int | None
    e1: int | None

    @classmethod
    def from_record(cls, rec: UsageRecord, provider: str, identity: str, label: str) -> LedgerRow:
        return cls(
            key=rec.lkey,
            provider=provider,
            identity=identity,
            label=label,
            ts_ms=round(rec.ts * 1000),
            model=rec.model_raw,
            inp=rec.input_tokens,
            outp=rec.output_tokens,
            cr=rec.cache_read,
            cc=rec.cache_creation,
            e5=rec._eph_5m,
            e1=rec._eph_1h,
        )


def orphan_record(
    row: tuple,
    *,
    provider: str,
    label: str,
    model_raw: str,
    pricing: dict[str, dict[str, float]],
) -> UsageRecord:
    """Rebuild a UsageRecord from a stored row, priced with the *current* table.

    Mirrors the parser's own derivations (model_norm fallback per provider, `known`
    from rate availability, cost from `compute_cost` with the stored sub-buckets), so a
    row costs exactly what the live record would under the same pricing."""
    key, _acct, ts_ms, _model, inp, outp, cr, cc, e5, e1 = row
    rates = get_rates(model_raw, pricing)
    fallback = UNATTRIBUTED if provider == CODEX_PROVIDER else "(unknown)"
    return UsageRecord(
        ts=ts_ms / 1000,
        model_raw=model_raw,
        model_norm=normalize_model(model_raw) or fallback,
        known=rates is not None,
        input_tokens=inp,
        output_tokens=outp,
        cache_read=cr,
        cache_creation=cc,
        cost=compute_cost(
            input_tokens=inp,
            output_tokens=outp,
            cache_read=cr,
            cache_creation_total=cc,
            ephemeral_5m=e5,
            ephemeral_1h=e1,
            rates=rates,
        ),
        _eph_5m=e5,
        _eph_1h=e1,
        account=label,
        provider=provider,
        lkey=key,
    )


@dataclass
class SourceResult:
    """What happened to one queued recovery source."""

    name: str
    why: str  # "damaged" (the moved-aside ledger), "missing" or "held" (a backup)
    status: str  # "merged", "unreadable", "failed" (retried), "refused", "gone"
    rows: int = 0  # rows read back and merged
    new_rows: int = 0  # of those, rows this ledger did not have yet
    complete: bool = False  # every part of the file was readable (quick_check ok)
    reason: str = ""
    mtime: float | None = None
    kept_as: str | None = None  # a backup slot file set aside instead of merged


@dataclass
class RecoveryReport:
    """The outcome of one pass over the recovery queue."""

    sources: list[SourceResult]
    still_pending: list[str]
    directory: Path | None = None  # where the sources live (named in full when damaged)

    @property
    def merged_any(self) -> bool:
        return any(source.new_rows for source in self.sources)

    def describe(self) -> str:
        """One plain sentence for the panel: what happened and whether history is
        intact, partly lost, or not recovered yet."""
        sources = self.sources
        damaged = [s for s in sources if s.why == "damaged"]
        parts: list[str] = []
        if damaged:
            where = self.directory / damaged[0].name if self.directory else damaged[0].name
            parts.append(f"the usage ledger was damaged and moved to {where}")
        elif any(s.why == "missing" for s in sources):
            parts.append("the usage ledger was missing, so a new one was started")
        for s in sources:
            when = (
                f" (from {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.mtime))})"
                if s.mtime is not None and s.why != "damaged"
                else ""
            )
            if s.status == "merged":
                extent = "all of it" if s.complete else "parts of it were unreadable"
                parts.append(f"recovered {s.rows:,} rows from {s.name}{when}, {extent}")
            elif s.status == "unreadable":
                parts.append(f"nothing in {s.name} was readable")
            elif s.status == "failed":
                parts.append(
                    f"could NOT read {s.name} yet ({s.reason}); its history is safe in "
                    "that file, ccusage retries on every scan and makes no backup until "
                    "it succeeds"
                )
            elif s.status == "refused":
                parts.append(f"did not merge {s.name} ({s.reason}); the file is kept")
            if s.kept_as:
                parts.append(f"{s.name} was set aside as {s.kept_as}")
        if any(s.status == "failed" for s in sources):
            parts.append("recovery is NOT complete")
        elif damaged and all(s.status == "merged" and s.complete for s in damaged):
            parts.append("history intact")
        elif any(s.status == "merged" and s.why != "damaged" for s in sources):
            parts.append(
                "usage recorded after that backup for transcripts already deleted may "
                "be lost" if not damaged else
                "usage recorded only in the unreadable part after that backup, for "
                "transcripts already deleted, may be lost"
            )
        elif damaged:
            parts.append(
                "there was no usable backup: history of deleted transcripts in the "
                "unreadable part is lost"
            )
        return "; ".join(parts)


class Ledger:
    """One process's connection to the shared ledger file.

    Opened lazily on first use (always from a worker thread) and then reused, so
    `PRAGMA data_version` can tell this connection whether *another* process has
    committed since. Callers serialize access (the engine holds a lock), hence
    ``check_same_thread=False``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.backup_prev = self.path.with_name(self.path.name + ".bak.prev")
        self._conn: sqlite3.Connection | None = None
        self._file_id: tuple[int, int] | None = None
        self._data_version: int | None = None
        # Set when this process ran a key-scheme migration: the engine then takes a
        # backup at once, so no backup is left behind on the old scheme.
        self.migrated = False

    # ── connection ───────────────────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _is_current(self) -> bool:
        """Whether the ledger path still names the file our connection has open.

        Another ccusage may have renamed an unreadable ledger aside and started a
        fresh one; a connection opened before that still points at the renamed file,
        and everything written through it would be lost to every later reader."""
        current = _file_id(self.path)
        return current is not None and current == self._file_id

    def ensure_current(self) -> bool:
        """Reconnect if the path now names a different file. True when it did (the
        caller should treat the ledger as new and re-sync everything it holds)."""
        if self._conn is None or self._is_current():
            return False
        self.close()
        return True

    def _open(self) -> sqlite3.Connection:
        if self._conn is not None:
            if self._is_current():
                return self._conn
            self.close()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LedgerUnavailable(f"cannot create {self.path.parent}: {exc}") from exc
        # Refuse a file that is not SQLite before SQLite touches it: closing a
        # connection to such a file makes SQLite delete its -wal/-shm, and those belong
        # with the damaged file when it is moved aside.
        try:
            with open(self.path, "rb") as fh:
                head = fh.read(len(_SQLITE_HEADER))
        except FileNotFoundError:
            head = b""
        except OSError as exc:
            raise LedgerUnavailable(f"cannot read {self.path}: {exc}") from exc
        if head and head != _SQLITE_HEADER:
            self._file_id = _file_id(self.path)
            raise LedgerCorrupt("file is not a database")
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise _classify(exc) from exc
        # Remember exactly which file we opened, so a corrupt-file rename never moves a
        # fresh ledger another process has already put in its place, and so a rename by
        # another process is noticed (see `_is_current`).
        self._file_id = _file_id(self.path)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA journal_size_limit = {_JOURNAL_SIZE_LIMIT}")
            self._ensure_schema(conn)
            self._ensure_key_scheme(conn)
            self._data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error as exc:
            conn.close()
            raise _classify(exc) from exc
        except LedgerError:
            conn.close()
            raise
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise LedgerUnavailable(
                f"ledger schema v{version} is newer than this ccusage understands "
                f"(v{SCHEMA_VERSION}); leaving it untouched"
            )
        if version < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-check under the write lock: another process may have just done it.
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    conn.execute(_META)
                    conn.execute(
                        "INSERT INTO meta (k, v) VALUES ('key_scheme', ?), ('ledger_id', ?)",
                        (str(KEY_SCHEME), uuid.uuid4().hex),
                    )
                    # A new ledger next to existing backups means the ledger went
                    # missing (or was just moved aside): their history comes back first.
                    backups = [
                        {"file": slot.name, "why": "missing"}
                        for slot in (self.backup_path, self.backup_prev)
                        if slot.exists()
                    ]
                    if backups:
                        _set_meta(conn, "pending", json.dumps(backups))
                elif version == 1:
                    # A pre-release ledger: scheme 0 by definition; migrated just below.
                    conn.execute(_META)
                    conn.execute(
                        "INSERT INTO meta (k, v) VALUES ('key_scheme', '0'), ('ledger_id', ?)",
                        (uuid.uuid4().hex,),
                    )
                if version < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise

    def _ensure_key_scheme(self, conn: sqlite3.Connection) -> None:
        """Run the key-scheme migrations this ledger needs, once, atomically."""
        stored = int(_meta(conn, "key_scheme") or 0)
        if stored == KEY_SCHEME:
            return
        if stored > KEY_SCHEME:
            raise LedgerUnavailable(
                f"ledger uses record key scheme v{stored}, newer than this ccusage "
                f"(v{KEY_SCHEME}); leaving it untouched"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = int(_meta(conn, "key_scheme") or 0)
            for version in range(stored, KEY_SCHEME):
                migrate = KEY_SCHEME_MIGRATIONS.get(version)
                if migrate is None:
                    raise LedgerUnavailable(
                        f"ledger uses record key scheme v{version} and there is no "
                        f"migration to v{version + 1}; leaving it untouched"
                    )
                migrate(conn)
            _set_meta(conn, "key_scheme", str(KEY_SCHEME))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        self.migrated = True

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def move_aside(self) -> Path | None:
        """Rename an unreadable ledger (and its WAL/SHM) to ``*.corrupt-<timestamp>``.

        Never deletes anything. Returns the new path, or None when the file at our path
        is no longer the one we found unreadable (another ccusage already moved it and
        started a fresh one — leave that alone). Raises `LedgerUnavailable` if the
        rename itself fails (e.g. Windows refusing while another process has it open)."""
        found = self._file_id
        self.close()
        self._file_id = None
        current = _file_id(self.path)
        if current is None or (found is not None and current != found):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        n = 2
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{n}")
            n += 1
        try:
            os.rename(self.path, target)
        except OSError as exc:
            raise LedgerUnavailable(
                f"ledger {self.path} is unreadable and could not be moved aside: {exc}"
            ) from exc
        # The WAL belongs to the damaged file: left behind, SQLite could replay it into
        # the fresh ledger. Moved alongside, it stays readable for the salvage.
        for suffix in ("-wal", "-shm"):
            side = Path(f"{self.path}{suffix}")
            if side.exists():
                try:
                    os.rename(side, Path(f"{target}{suffix}"))
                except OSError:
                    pass
        return target

    def changed_elsewhere(self) -> bool:
        """Whether another connection committed since this one last looked."""
        conn = self._open()
        try:
            version = conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise _classify(exc) from exc
        changed = version != self._data_version
        self._data_version = version
        return changed

    # ── writes ───────────────────────────────────────────────────────────────
    def write(self, rows: list[LedgerRow]) -> int:
        """Upsert `rows` in one transaction; returns how many rows were sent."""
        if not rows:
            return 0
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                accounts = self._intern_accounts(conn, rows)
                models = self._intern_models(conn, {_text(row.model) for row in rows})
                params = sorted(
                    (
                        row.key,
                        accounts[(_text(row.provider), _text(row.identity))],
                        row.ts_ms,
                        models[_text(row.model)],
                        row.inp,
                        row.outp,
                        row.cr,
                        row.cc,
                        row.e5,
                        row.e1,
                    )
                    for row in rows
                )
                # Key order turns a large backfill into appends: faster, denser pages.
                conn.executemany(_UPSERT.format(u=int(models[UNATTRIBUTED])), params)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        except sqlite3.Error as exc:
            raise _classify(exc) from exc
        return len(rows)

    @staticmethod
    def _intern_accounts(
        conn: sqlite3.Connection, rows: list[LedgerRow]
    ) -> dict[tuple[str, str], int]:
        labels: dict[tuple[str, str], str] = {}
        for row in rows:
            labels[(_text(row.provider), _text(row.identity))] = _text(row.label)
        # The label is the one current when these rows were written (R5); a later
        # rename updates it, but the identity — and so the account — stays the same.
        conn.executemany(
            "INSERT INTO accounts (provider, identity, label) VALUES (?, ?, ?) "
            "ON CONFLICT (provider, identity) DO UPDATE SET label = excluded.label "
            "WHERE accounts.label != excluded.label",
            [(provider, identity, label) for (provider, identity), label in labels.items()],
        )
        return {
            (provider, identity): account_id
            for account_id, provider, identity in conn.execute(
                "SELECT id, provider, identity FROM accounts"
            )
        }

    @staticmethod
    def _intern_models(conn: sqlite3.Connection, names: set[str]) -> dict[str, int]:
        conn.executemany(
            "INSERT OR IGNORE INTO models (name) VALUES (?)",
            [(name,) for name in sorted(names | {UNATTRIBUTED})],
        )
        return {name: model_id for model_id, name in conn.execute("SELECT id, name FROM models")}

    # ── reads ────────────────────────────────────────────────────────────────
    def all_rows(self) -> Iterator[tuple]:
        """Stream every stored row (``_ROW_COLUMNS`` order) without holding them all."""
        conn = self._open()
        try:
            yield from conn.execute(f"SELECT {_ROW_COLUMNS} FROM usage")
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def rows(self, keys: list[int]) -> list[tuple]:
        """Full stored rows for `keys`, in the column order `orphan_record` expects."""
        if not keys:
            return []
        conn = self._open()
        try:
            if len(keys) > _SCAN_ALL_OVER:
                wanted = set(keys)
                return [
                    row
                    for row in conn.execute(f"SELECT {_ROW_COLUMNS} FROM usage")
                    if row[0] in wanted
                ]
            out: list[tuple] = []
            for start in range(0, len(keys), _IN_BATCH):
                batch = keys[start : start + _IN_BATCH]
                marks = ",".join("?" * len(batch))
                out.extend(
                    conn.execute(
                        f"SELECT {_ROW_COLUMNS} FROM usage WHERE key IN ({marks})", batch
                    )
                )
            return out
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def accounts(self) -> dict[int, tuple[str, str, str]]:
        """Account id -> (provider, identity, last label)."""
        conn = self._open()
        try:
            return {
                account_id: (provider, identity, label)
                for account_id, provider, identity, label in conn.execute(
                    "SELECT id, provider, identity, label FROM accounts"
                )
            }
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def models(self) -> dict[int, str]:
        conn = self._open()
        try:
            return dict(conn.execute("SELECT id, name FROM models"))
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    # ── backup and recovery ──────────────────────────────────────────────────
    def lineage(self) -> str | None:
        conn = self._open()
        try:
            return _meta(conn, "ledger_id")
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def merged_lineages(self) -> set[str]:
        conn = self._open()
        try:
            return {str(v) for v in _json_list(_meta(conn, "merged"))}
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def pending(self) -> list[dict]:
        """Recovery sources still to merge: [{"file": name, "why": reason}, ...]."""
        conn = self._open()
        try:
            raw = _json_list(_meta(conn, "pending"))
        except sqlite3.Error as exc:
            raise _classify(exc) from exc
        return [e for e in raw if isinstance(e, dict) and isinstance(e.get("file"), str)]

    def _update_meta_lists(
        self,
        *,
        add_pending: list[dict] = (),
        drop_pending: set[str] = frozenset(),
        add_merged: set[str] = frozenset(),
    ) -> None:
        """Edit the pending queue / merged set under the write lock (read-modify-write
        against whatever another process wrote meanwhile)."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pending = [
                    e
                    for e in _json_list(_meta(conn, "pending"))
                    if isinstance(e, dict) and e.get("file") not in drop_pending
                ]
                names = {e.get("file") for e in pending}
                front = [e for e in add_pending if e["file"] not in names]
                _set_meta(conn, "pending", json.dumps(front + pending))
                if add_merged:
                    merged = {str(v) for v in _json_list(_meta(conn, "merged"))} | add_merged
                    _set_meta(conn, "merged", json.dumps(sorted(merged)))
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        except sqlite3.Error as exc:
            raise _classify(exc) from exc

    def queue(self, name: str, why: str) -> None:
        """Queue a file in the ledger's directory for recovery (idempotent)."""
        self._update_meta_lists(add_pending=[{"file": name, "why": why}])

    def recover(self, moved: Path) -> RecoveryReport:
        """Queue a moved-aside ledger (after the backups a new ledger queues itself)
        and run the queue once."""
        self.queue(moved.name, "damaged")
        return self.process_pending()

    def process_pending(self) -> RecoveryReport | None:
        """Merge every queued recovery source that can be read now.

        A source read successfully leaves the queue; a damaged or refused backup slot is
        renamed aside (kept) so the slot can hold a new backup; a complete backup's
        lineage is recorded as merged. A source that could not be read for an
        environmental reason stays queued. Write errors propagate (the queue is kept)."""
        entries = self.pending()
        if not entries:
            return None
        results: list[SourceResult] = []
        done: set[str] = set()
        merged: set[str] = set()
        slots = {self.backup_path.name, self.backup_prev.name}
        for entry in entries:
            name, why = entry["file"], str(entry.get("why") or "held")
            path = self.path.parent / name
            if not path.exists():
                results.append(SourceResult(name, why, "gone"))
                done.add(name)
                continue
            try:
                result, lineage = self._merge_source(path, why)
            except RecoveryFailed as exc:
                results.append(SourceResult(name, why, "failed", reason=str(exc)))
                continue
            except SourceRefused as exc:
                result, lineage = SourceResult(name, why, "refused", reason=str(exc)), None
            done.add(name)
            fully = result.status == "merged" and result.complete
            if fully and lineage:
                merged.add(lineage)
            if name in slots and not (fully and lineage):
                # A slot may only keep a file a later backup can recognise as covered;
                # anything else is renamed aside (kept) so it can never block, or be
                # overwritten by, a rotation.
                label = (
                    "unmerged" if result.status == "refused"
                    else "merged" if fully
                    else "damaged"
                )
                result.kept_as = self._set_aside(path, label)
            results.append(result)
        self._update_meta_lists(drop_pending=done, add_merged=merged)
        return RecoveryReport(results, [e["file"] for e in self.pending()], self.path.parent)

    def _set_aside(self, path: Path, label: str) -> str | None:
        """Rename a file to ``<name>.<label>-<timestamp>`` (never delete it)."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.{label}-{stamp}")
        n = 2
        while target.exists():
            target = path.with_name(f"{path.name}.{label}-{stamp}-{n}")
            n += 1
        try:
            os.rename(path, target)
        except OSError:
            return None
        return target.name

    def _merge_source(self, path: Path, why: str) -> tuple[SourceResult, str | None]:
        source = _read_source(path, self.path.parent)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if source.unreadable:
            return SourceResult(path.name, why, "unreadable", mtime=mtime), None
        if source.accounts is None or source.models is None:
            # Its id tables are damaged: a backup of the same lineage can stand in.
            for slot in (self.backup_path, self.backup_prev):
                if slot == path or not slot.exists():
                    continue
                tables = _read_tables(slot)
                if tables is not None and source.ledger_id and tables[0] == source.ledger_id:
                    source.accounts = {**tables[1], **(source.accounts or {})}
                    source.models = {**tables[2], **(source.models or {})}
                    break
        rows = _migrated_rows(source, path.name)
        present = {row[0] for row in self.rows([row.key for row in rows])}
        self.write(rows)
        result = SourceResult(
            path.name,
            why,
            "merged",
            rows=len(rows),
            new_rows=sum(1 for row in rows if row.key not in present),
            complete=source.complete,
            mtime=mtime,
        )
        return result, source.ledger_id

    def backup_due(self, now: float | None = None) -> bool:
        try:
            age = (time.time() if now is None else now) - self.backup_path.stat().st_mtime
        except OSError:
            return True
        return age >= BACKUP_INTERVAL_SECS

    def backup(self) -> bool:
        """Rotate backups: verify a fresh copy, move ``.bak`` to ``.bak.prev``, install
        the copy as ``.bak``. Returns False when it had to hold off.

        It holds off while the recovery queue is not empty, and when a backup slot holds
        a file that is not covered (its lineage is neither this ledger's nor one it has
        merged — or it is unreadable): that file is queued for merging instead, and the
        rotation happens on a later sync once it is covered. So the only good copy of any
        history is never overwritten. Worker threads only."""
        if self.pending():
            return False
        covered = {self.lineage()} | self.merged_lineages()
        held = []
        for slot in (self.backup_path, self.backup_prev):
            if slot.exists():
                tables = _read_tables(slot)
                if tables is None or tables[0] not in covered:
                    held.append(slot.name)
        if held:
            for name in held:
                self.queue(name, "held")
            return False
        conn = self._open()
        tmp = self.backup_path.with_name(f"{self.backup_path.name}.{os.getpid()}.tmp")
        try:
            target = sqlite3.connect(str(tmp), isolation_level=None)
            try:
                conn.backup(target)
                target.execute("PRAGMA journal_mode = DELETE")  # one self-contained file
                check = target.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                target.close()
            if check != "ok":
                raise LedgerCorrupt(f"backup copy failed its integrity check: {check}")
            if self.backup_path.exists():
                os.replace(self.backup_path, self.backup_prev)
            os.replace(tmp, self.backup_path)
        except sqlite3.Error as exc:
            raise _classify(exc) from exc
        except OSError as exc:
            raise LedgerUnavailable(f"cannot write {self.backup_path}: {exc}") from exc
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return True


# ── salvage ────────────────────────────────────────────────────────────────────
_KEY_MIN, _KEY_MAX = -(2**63), 2**63 - 1
_TS_MAX = 4_102_444_800_000  # 2100-01-01 in ms: anything later is not a real record
_TOKENS_MAX = 2**53


@dataclass
class _Salvaged:
    raw: list[tuple]
    accounts: dict[int, tuple[str, str, str]] | None
    models: dict[int, str] | None
    ledger_id: str | None
    key_scheme: int | None  # None: unknown (its meta could not be read)
    complete: bool
    unreadable: bool = False

    def to_rows(self) -> list[LedgerRow]:
        rows: list[LedgerRow] = []
        accounts = self.accounts or {}
        models = self.models or {}
        for key, acct, ts, model, inp, outp, cr, cc, e5, e1 in self.raw:
            account = accounts.get(acct)
            name = models.get(model)
            if account is None or name is None:
                self.complete = False
                continue
            provider, identity, label = account
            rows.append(
                LedgerRow(key, provider, identity, label, ts, name, inp, outp, cr, cc, e5, e1)
            )
        return rows


def _plausible(row: tuple) -> bool:
    """Reject rows a damaged page could have produced that no real record can have."""
    if len(row) != 10:
        return False
    key, acct, ts, model, inp, outp, cr, cc, e5, e1 = row
    if not all(isinstance(v, int) for v in (key, acct, ts, model, inp, outp, cr, cc)):
        return False
    if not 0 < ts < _TS_MAX:
        return False
    if not all(0 <= v < _TOKENS_MAX for v in (inp, outp, cr, cc)):
        return False
    return all(v is None or (isinstance(v, int) and 0 <= v < _TOKENS_MAX) for v in (e5, e1))


def _read_tables(path: Path) -> tuple[str | None, dict, dict] | None:
    """(ledger_id, accounts, models) of an intact ledger/backup file, read-only; None if
    any of it cannot be read."""
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        lineage = _meta(conn, "ledger_id")
        accounts = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT id, provider, identity, label FROM accounts")
        }
        models = dict(conn.execute("SELECT id, name FROM models"))
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return lineage, accounts, models


def _read_source(path: Path, scratch_parent: Path) -> _Salvaged:
    """Read whatever a (possibly damaged) ledger or backup file still yields.

    Works on a scratch copy — made next to the ledger, never in TMPDIR, which may be a
    small RAM disk — so the source is never modified. Any failure to make or open that
    copy is environmental and raises `RecoveryFailed`: the caller must not mistake it
    for "nothing to recover"."""
    try:
        holder = tempfile.TemporaryDirectory(
            prefix=".ccusage-salvage-", dir=scratch_parent, ignore_cleanup_errors=True
        )
    except OSError as exc:
        raise RecoveryFailed(f"cannot make a scratch copy of {path.name}: {exc}") from exc
    with holder as scratch:
        copy = Path(scratch) / "source.sqlite3"
        try:
            shutil.copyfile(path, copy)
            for suffix in ("-wal", "-shm"):
                side = Path(f"{path}{suffix}")
                if side.exists():
                    shutil.copyfile(side, Path(f"{copy}{suffix}"))
        except OSError as exc:
            raise RecoveryFailed(f"could not copy {path.name}: {exc}") from exc
        try:
            conn = sqlite3.connect(str(copy), isolation_level=None)
        except sqlite3.Error as exc:
            raise RecoveryFailed(f"could not open a copy of {path.name}: {exc}") from exc
        try:
            return _salvage_connection(conn, path.name)
        finally:
            conn.close()


# Salvage narrows an unreadable key range down to this width before giving up on it,
# and stops splitting after this many failed reads (a wholly unreadable table would
# otherwise cost ~a million probes).
_SALVAGE_MIN_WIDTH = 2**46
_SALVAGE_MAX_FAILURES = 20_000


def _salvage_connection(conn: sqlite3.Connection, name: str) -> _Salvaged:
    def damage_or_raise(exc: sqlite3.Error) -> None:
        """Damage is expected and survivable; anything else (I/O, disk full, out of
        memory) means the read itself failed and must be retried later."""
        if not isinstance(_classify(exc), LedgerCorrupt):
            raise RecoveryFailed(f"reading {name} failed: {exc}") from exc

    try:
        # Validate each cell's size as it is read, so a damaged page raises instead of
        # silently yielding fewer rows.
        conn.execute("PRAGMA cell_size_check = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        damage_or_raise(exc)
        return _Salvaged([], None, None, None, None, False, unreadable=True)
    complete = True

    def read(sql: str) -> list[tuple] | None:
        nonlocal complete
        try:
            return list(conn.execute(sql))
        except sqlite3.Error as exc:
            damage_or_raise(exc)
            complete = False
            return None

    account_rows = read("SELECT id, provider, identity, label FROM accounts")
    model_rows = read("SELECT id, name FROM models")
    meta = None
    if "meta" in tables:
        meta_rows = read("SELECT k, v FROM meta")
        meta = dict(meta_rows) if meta_rows is not None else None
    accounts = (
        {row[0]: (row[1], row[2], row[3]) for row in account_rows}
        if account_rows is not None
        else None
    )
    models = {row[0]: row[1] for row in model_rows} if model_rows is not None else None
    ledger_id = meta.get("ledger_id") if meta else None
    if "meta" not in tables:
        key_scheme = 0 if version == 1 else None  # the pre-release layout had no meta
    elif meta is None or "key_scheme" not in meta:
        key_scheme = None
    else:
        try:
            key_scheme = int(meta["key_scheme"])
        except ValueError:
            key_scheme = None

    raw: list[tuple] = []
    step = 2**58  # 64 top-level ranges over the signed 64-bit key space
    pending = [(lo, min(lo + step - 1, _KEY_MAX)) for lo in range(_KEY_MIN, _KEY_MAX, step)]
    failures = 0
    while pending:
        lo, hi = pending.pop()
        try:
            part = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM usage WHERE key BETWEEN ? AND ?", (lo, hi)
            ).fetchall()
        except sqlite3.Error as exc:
            damage_or_raise(exc)
            failures += 1
            width = hi - lo + 1
            if width <= _SALVAGE_MIN_WIDTH or failures > _SALVAGE_MAX_FAILURES:
                complete = False  # this range sits on an unreadable page
                continue
            sub = width // 16
            pending.extend(
                (lo + i * sub, hi if i == 15 else lo + (i + 1) * sub - 1) for i in range(16)
            )
            continue
        for row in part:
            if _plausible(row):
                raw.append(row)
            else:
                complete = False
    # A range scan can skip cells on some damaged pages without any error. Only a
    # clean integrity check lets the salvage call itself complete.
    try:
        check = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        damage_or_raise(exc)
        check = []
    if check != [("ok",)]:
        complete = False
    return _Salvaged(raw, accounts, models, ledger_id, key_scheme, complete)


def _migrated_rows(source: _Salvaged, name: str) -> list[LedgerRow]:
    """The source's rows under the *current* key scheme, or `SourceRefused`.

    Rows from an older scheme are rebuilt in a clean in-memory ledger and run through
    `KEY_SCHEME_MIGRATIONS` exactly as a live ledger would be, so recovery can never
    re-import rows the compatibility rule says must be re-keyed or dropped."""
    scheme = source.key_scheme
    if scheme is None:
        raise SourceRefused(f"the record key scheme of {name} is unreadable")
    if scheme > KEY_SCHEME:
        raise SourceRefused(f"{name} uses record key scheme v{scheme}, newer than this ccusage")
    if scheme < KEY_SCHEME:
        missing = [v for v in range(scheme, KEY_SCHEME) if v not in KEY_SCHEME_MIGRATIONS]
        if missing:
            raise SourceRefused(
                f"{name} uses record key scheme v{missing[0]} and there is no migration"
            )
        try:
            mem = sqlite3.connect(":memory:", isolation_level=None)
            try:
                for statement in _SCHEMA:
                    mem.execute(statement)
                mem.executemany(
                    "INSERT INTO accounts (id, provider, identity, label) VALUES (?, ?, ?, ?)",
                    [(i, *a) for i, a in (source.accounts or {}).items()],
                )
                mem.executemany(
                    "INSERT INTO models (id, name) VALUES (?, ?)",
                    list((source.models or {}).items()),
                )
                mem.executemany(
                    f"INSERT OR IGNORE INTO usage ({_ROW_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    source.raw,
                )
                mem.execute("BEGIN")
                for version in range(scheme, KEY_SCHEME):
                    KEY_SCHEME_MIGRATIONS[version](mem)
                mem.execute("COMMIT")
                source.raw = list(mem.execute(f"SELECT {_ROW_COLUMNS} FROM usage"))
            finally:
                mem.close()
        except sqlite3.Error as exc:
            raise RecoveryFailed(f"migrating the rows of {name} failed: {exc}") from exc
    return source.to_rows()


# ── read-only inspection (`ccusage --ledger-info`) ─────────────────────────────
@dataclass
class LedgerSummary:
    path: Path
    size_bytes: int
    rows: int
    rows_by_provider: dict[str, int]
    # (label, provider, identity, rows) per account, most rows first.
    accounts: list[tuple[str, str, str, int]]
    first_ts: float | None
    last_ts: float | None
    keys: dict[int, int]  # key -> account id
    account_ids: dict[int, tuple[str, str, str]]
    backup_time: float | None = None
    pending: list[str] | None = None  # recovery sources not merged yet


def ledger_files_size(path: Path) -> int:
    """Bytes on disk for the ledger plus its WAL/SHM side files."""
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def read_summary(path: Path, *, retry_delay: float = 0.5) -> LedgerSummary:
    """Summarise the ledger through a strictly read-only connection. Raises LedgerError.

    Always SQLite's ``mode=ro`` (never ``immutable``): a running ccusage may be
    checkpointing, and only a real read-only connection sees a consistent snapshot of
    the file plus its WAL. A failure is retried once before it is reported, so a
    transient lock or a mid-checkpoint read never calls a healthy ledger unreadable.
    Opening read-only may create SQLite's empty -wal/-shm index files; the ledger's
    contents are never changed."""
    try:
        return _read_summary_once(path)
    except LedgerError:
        time.sleep(retry_delay)
        return _read_summary_once(path)


def _read_summary_once(path: Path) -> LedgerSummary:
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise _classify(exc) from exc
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise LedgerUnavailable(
                f"ledger schema v{version}; this ccusage reads v{SCHEMA_VERSION} "
                "(launch this ccusage once to upgrade it)"
            )
        scheme = int(_meta(conn, "key_scheme") or 0)
        if scheme != KEY_SCHEME:
            raise LedgerUnavailable(
                f"ledger uses record key scheme v{scheme}; this ccusage uses v{KEY_SCHEME}"
            )
        account_ids = {
            account_id: (provider, identity, label)
            for account_id, provider, identity, label in conn.execute(
                "SELECT id, provider, identity, label FROM accounts"
            )
        }
        pending = [
            str(e.get("file"))
            for e in _json_list(_meta(conn, "pending"))
            if isinstance(e, dict) and e.get("file")
        ]
        keys = dict(conn.execute("SELECT key, acct FROM usage"))
        per_account: dict[int, int] = {}
        for account_id in keys.values():
            per_account[account_id] = per_account.get(account_id, 0) + 1
        first, last = conn.execute("SELECT min(ts), max(ts) FROM usage").fetchone()
    except sqlite3.Error as exc:
        raise _classify(exc) from exc
    finally:
        conn.close()
    by_provider: dict[str, int] = {}
    accounts: list[tuple[str, str, str, int]] = []
    for account_id, count in per_account.items():
        provider, identity, label = account_ids.get(account_id, ("?", "?", "?"))
        by_provider[provider] = by_provider.get(provider, 0) + count
        accounts.append((label, provider, identity, count))
    accounts.sort(key=lambda item: (-item[3], item[0]))
    try:
        backup_time = Path(f"{path}.bak").stat().st_mtime
    except OSError:
        backup_time = None
    return LedgerSummary(
        path=Path(path),
        size_bytes=ledger_files_size(Path(path)),
        rows=len(keys),
        rows_by_provider=by_provider,
        accounts=accounts,
        first_ts=first / 1000 if first is not None else None,
        last_ts=last / 1000 if last is not None else None,
        keys=keys,
        account_ids=account_ids,
        backup_time=backup_time,
        pending=pending,
    )
