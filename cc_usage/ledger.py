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
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .accounts import CODEX_PROVIDER
from .cost import compute_cost, get_rates, normalize_model
from .parser import UsageRecord

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
# Keep the WAL from lingering at the size of the first (backfill) transaction.
_JOURNAL_SIZE_LIMIT = 4 * 1024 * 1024
UNATTRIBUTED = "codex-unattributed"
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


class LedgerError(Exception):
    """The ledger could not be used for this operation (never fatal to the panel)."""


class LedgerBusy(LedgerError):
    """Another process held the database past the busy timeout; retry next scan."""


class LedgerCorrupt(LedgerError):
    """The file is not a readable SQLite database (it gets moved aside, not deleted)."""


class LedgerUnavailable(LedgerError):
    """Disk full, read-only or unopenable location, or a newer schema: run without it."""


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


class Ledger:
    """One process's connection to the shared ledger file.

    Opened lazily on first use (always from a worker thread) and then reused, so
    `PRAGMA data_version` can tell this connection whether *another* process has
    committed since. Callers serialize access (the engine holds a lock), hence
    ``check_same_thread=False``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._file_id: tuple[int, int] | None = None
        self._data_version: int | None = None

    # ── connection ───────────────────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _open(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LedgerUnavailable(f"cannot create {self.path.parent}: {exc}") from exc
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
        # fresh ledger another process has already put in its place.
        self._file_id = _file_id(self.path)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA journal_size_limit = {_JOURNAL_SIZE_LIMIT}")
            self._ensure_schema(conn)
            self._data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error as exc:
            conn.close()
            raise _classify(exc) from exc
        except LedgerError:
            conn.close()
            raise
        self._conn = conn
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise LedgerUnavailable(
                f"ledger schema v{version} is newer than this ccusage understands "
                f"(v{SCHEMA_VERSION}); leaving it untouched"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have just created it.
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

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
                models = self._intern_models(conn, {row.model for row in rows})
                params = sorted(
                    (
                        row.key,
                        accounts[(row.provider, row.identity)],
                        row.ts_ms,
                        models[row.model],
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
            labels[(row.provider, row.identity)] = row.label
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
    def key_accounts(self) -> dict[int, int]:
        """Every stored key -> its account id (the cheap half of the orphan diff)."""
        conn = self._open()
        try:
            return dict(conn.execute("SELECT key, acct FROM usage"))
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


def ledger_files_size(path: Path) -> int:
    """Bytes on disk for the ledger plus its WAL/SHM side files."""
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def read_summary(path: Path) -> LedgerSummary:
    """Open the ledger strictly read-only and summarise it. Raises LedgerError.

    With a WAL present (a ccusage is running, or one crashed mid-write) the read goes
    through SQLite's read-only mode so recent commits are included. Without one, no
    connection is open and the file is fully checkpointed, so it is read as immutable:
    a read-only WAL open would otherwise leave empty -wal/-shm files behind."""
    base = Path(path).resolve().as_uri()
    uri = f"{base}?mode=ro" if Path(f"{path}-wal").exists() else f"{base}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise _classify(exc) from exc
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise LedgerUnavailable(
                f"ledger schema v{version} is not v{SCHEMA_VERSION} (written by another ccusage?)"
            )
        account_ids = {
            account_id: (provider, identity, label)
            for account_id, provider, identity, label in conn.execute(
                "SELECT id, provider, identity, label FROM accounts"
            )
        }
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
    )
