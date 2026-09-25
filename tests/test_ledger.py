"""Durable usage ledger (T17): history survives transcript deletion.

The guarantee under test: a usage record ccusage has seen once stays in every total,
window, chart, by-model, by-account and date-range view after its transcript is
deleted, moved or truncated — across restarts, cache rebuilds and pricing changes —
and nothing counts twice. Hermetic: synthetic Claude/Codex roots under tmp_path, a tmp
ledger, pricing and discovery stubbed; nothing reads ~/.claude or ~/.codex.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import cc_usage.engine as engine_module
import cc_usage.ledger as ledger_module
import cc_usage.parser as parser_module
from cc_usage.accounts import CODEX_PROVIDER, Root
from cc_usage.cli import main as cli_main
from cc_usage.config import Config
from cc_usage.cost import compute_cost, get_rates
from cc_usage.engine import Engine
from cc_usage.ledger import LedgerUnavailable
from cc_usage.parser import Parser

NOW = 1_780_000_000.0  # 2026-05-28, a fixed "now" so every window is deterministic
H = 3600.0
D = 86400.0
PRICING = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "gpt-test": {"input": 2.0, "output": 8.0},
}
ROLLOUT = "rollout-2026-05-27T10-00-00-019e71bb-f375-7731-9644-9a9412399f58.jsonl"
SESSION = "019e71bb-f375-7731-9644-9a9412399f58"


def _iso(ts: float) -> str:
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def claude_line(
    req: str,
    mid: str,
    ts: float,
    inp: int,
    out: int = 0,
    *,
    model: str = "claude-opus-4-8",
    cache_read: int = 0,
    cache_creation: int = 0,
    eph: tuple[int, int] | None = None,
    text: str = "hello there",
    cwd: str = "/work/alpha",
    branch: str = "main",
) -> str:
    usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if eph is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": eph[0],
            "ephemeral_1h_input_tokens": eph[1],
        }
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "uuid": f"uuid-{mid}-{out}",
                "timestamp": _iso(ts),
                "cwd": cwd,
                "gitBranch": branch,
                "message": {
                    "id": mid,
                    "model": model,
                    "usage": usage,
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def codex_context(ts: float, model: str = "gpt-test", cwd: str = "/work/codex") -> str:
    return (
        json.dumps(
            {"timestamp": _iso(ts), "type": "turn_context", "payload": {"model": model, "cwd": cwd}}
        )
        + "\n"
    )


def codex_tokens(ts: float, total: tuple[int, int, int], last: tuple[int, int, int]) -> str:
    def usage(t):
        return {
            "input_tokens": t[0],
            "cached_input_tokens": t[1],
            "output_tokens": t[2],
            "total_tokens": t[0] + t[2],
        }

    return (
        json.dumps(
            {
                "timestamp": _iso(ts),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": usage(total), "last_token_usage": usage(last)},
                },
            }
        )
        + "\n"
    )


def codex_message(ts: float, text: str) -> str:
    return (
        json.dumps(
            {
                "timestamp": _iso(ts),
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"text": text}]},
            }
        )
        + "\n"
    )


class World:
    """A synthetic machine: Claude roots `personal` + `company`, Codex root `codex`."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.base = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.ledger = self.state / "ledger.sqlite3"
        self.pricing = dict(PRICING)
        self.personal = tmp_path / ".claude"
        self.company = tmp_path / ".claude-company"
        self.codex = tmp_path / ".codex"
        self.alpha = self.personal / "projects" / "-work-alpha"
        self.beta = self.company / "projects" / "-work-beta"
        self.sessions = self.codex / "sessions" / "2026" / "05" / "27"
        self.archive = self.codex / "archived_sessions"
        for d in (self.alpha / "sub", self.beta, self.sessions, self.archive):
            d.mkdir(parents=True)
        self.claude_roots = [
            Root("personal", self.personal, self.personal / "projects", "auto"),
            Root("company", self.company, self.company / "projects", "config"),
        ]
        self.codex_roots = [Root("codex", self.codex, self.codex / "sessions", "auto")]
        monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: self.claude_roots)
        monkeypatch.setattr(
            engine_module, "discover_codex_roots", lambda *a, **k: self.codex_roots
        )
        monkeypatch.setattr(engine_module, "load_pricing", lambda: (dict(self.pricing), []))
        monkeypatch.setattr(engine_module, "LIMITS_CACHE_JSON", tmp_path / "limits.json")
        # A single default root pair makes the engine fall back to the parser's legacy
        # PROJECTS_DIR scan; keep that pointed inside tmp_path, never at ~/.claude.
        monkeypatch.setattr(parser_module, "PROJECTS_DIR", self.personal / "projects")

    def populate(self) -> None:
        (self.alpha / "s1.jsonl").write_text(
            # A streaming pair: partial then final output for one message (T9).
            claude_line("r1", "m1", NOW - 0.5 * H, 1000, 5, cache_creation=1200, eph=(1000, 200))
            + claude_line(
                "r1", "m1", NOW - 0.5 * H, 1000, 500, cache_creation=1200, eph=(1000, 200)
            )
            # No sub-bucket object: compute_cost's 1.25x aggregate fallback (NULL, not 0).
            + claude_line(
                "r2", "m2", NOW - 3 * H, 300, 40, model="claude-sonnet-4-6", cache_creation=300
            )
            # An unpriced model: tokens counted, flagged, cost excluded.
            + claude_line("r3", "m3", NOW - 2 * D, 50, 5, model="claude-mystery-1", cache_read=900),
            "utf-8",
        )
        (self.alpha / "sub" / "agent-1.jsonl").write_text(
            claude_line("r4", "m4", NOW - 20 * D, 700, 70, cache_read=5000), "utf-8"
        )
        (self.beta / "s2.jsonl").write_text(
            claude_line("r5", "m5", NOW - 6 * H, 2000, 200, cwd="/work/beta")
            + claude_line("r6", "m6", NOW - 10 * D, 800, 80, cwd="/work/beta"),
            "utf-8",
        )
        (self.sessions / ROLLOUT).write_text(self.rollout_text(), "utf-8")

    @staticmethod
    def rollout_text() -> str:
        t0 = NOW - 5 * H
        return (
            # A token_count before the first turn_context -> codex-unattributed, then
            # re-attributed once the model marker arrives.
            codex_tokens(t0, (100, 20, 10), (100, 20, 10))
            + codex_context(t0 + 1)
            + codex_tokens(t0 + 60, (400, 120, 60), (300, 100, 50))
            # Codex re-emits an event, at the same and at a later timestamp: the
            # cumulative total did not move, so neither is new usage.
            + codex_tokens(t0 + 60, (400, 120, 60), (300, 100, 50))
            + codex_tokens(t0 + 90, (400, 120, 60), (300, 100, 50))
            # Same timestamp as the re-emission, counters advanced: a distinct event.
            + codex_tokens(t0 + 60, (500, 150, 70), (100, 30, 10))
        )

    def engine(self, cache: str | None = "cache.pkl", ledger: bool = True) -> Engine:
        cache_path = self.state / cache if cache is not None else None
        return Engine(Config(), cache_path=cache_path, ledger_path=self.ledger if ledger else None)

    def scanned(self, cache: str | None = "cache.pkl", ledger: bool = True) -> Engine:
        eng = self.engine(cache, ledger)
        eng.scan()
        eng.sync_ledger()
        eng.save_cache()
        return eng


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    w = World(tmp_path, monkeypatch)
    w.populate()
    return w


def views(eng: Engine, now: float = NOW) -> dict:
    """Every user-facing aggregate: windows (+ by-model), by-account, heartbeat for
    all windows and both metrics, the date-range view and the unpriced footnote."""

    def window(w):
        models = sorted(
            (m.model, m.known, m.input_tokens, m.output_tokens, m.cache_tokens, m.cost)
            for m in w.models.values()
        )
        return (w.input_tokens, w.output_tokens, w.cache_tokens, w.unpriced_tokens, w.cost, models)

    snap = eng.snapshot(now=now)
    out = {
        "windows": {name: window(w) for name, w in snap.windows.items()},
        "accounts": sorted(
            (a.label, a.is_codex, a.input_tokens, a.output_tokens, a.cache_tokens, a.cost)
            for a in snap.accounts
        ),
        "unknown": sorted(snap.unknown_models),
        "records": len(eng.records),
    }
    saved = (eng.hb_window, eng.hb_metric)
    for hb_window in ("5h", "24h", "7d"):
        for metric in ("cost", "tokens"):
            eng.hb_window, eng.hb_metric = hb_window, metric
            out[f"hb:{hb_window}:{metric}"] = eng.snapshot(now=now).heartbeat.values
    eng.hb_window, eng.hb_metric = saved
    rng = eng.range_metrics(now - 30 * D, now)
    out["range"] = (
        rng.input_tokens,
        rng.output_tokens,
        rng.cache_tokens,
        rng.unpriced_tokens,
        rng.record_count,
        rng.cost,
        sorted(
            (m.model, m.input_tokens, m.output_tokens, m.cache_tokens, m.cost)
            for m in rng.models.values()
        ),
        [(d.date, d.input_tokens, d.output_tokens, d.cache_tokens, d.cost) for d in rng.days],
    )
    return out


def assert_same(a, b, path="views"):
    """Equal, with float sums compared to 1e-12 (orphans are appended after the live
    records, so a sum may add the same terms in a different order)."""
    if isinstance(a, float) or isinstance(b, float):
        assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12), (path, a, b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for key in a:
            assert_same(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, (list, tuple)):
        assert len(a) == len(b), (path, a, b)
        for i, (x, y) in enumerate(zip(a, b)):
            assert_same(x, y, f"{path}[{i}]")
    else:
        assert a == b, (path, a, b)


def ledger_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT u.key, a.provider, a.label, u.ts, m.name, "
            "u.inp, u.outp, u.cr, u.cc, u.e5, u.e1 "
            "FROM usage u JOIN accounts a ON a.id = u.acct JOIN models m ON m.id = u.model "
            "ORDER BY u.ts"
        ).fetchall()
    finally:
        conn.close()


def delete_some(world: World) -> None:
    """Retention deletes a Claude session in each root and the Codex rollout."""
    (world.alpha / "sub" / "agent-1.jsonl").unlink()
    (world.beta / "s2.jsonl").unlink()
    (world.sessions / ROLLOUT).unlink()


# ── 1. the guarantee ─────────────────────────────────────────────────────────────
def test_deleted_transcripts_stay_in_every_view_warm_and_cold(world):
    first = world.scanned()
    before = views(first)
    assert before["records"] == 9  # 6 Claude messages + 3 counted Codex events
    first.close()

    delete_some(world)

    # Warm: the next launch primes from the parse cache, loads ledger history before
    # the reconcile scan (as the TUI worker does), then the scan finds the deletions
    # and rebuilds.
    warm = world.engine()
    assert warm.prime_cache()
    warm.sync_ledger()
    assert_same(views(warm), before)
    warm.scan()
    assert len(warm.parser.records) == 3  # only s1.jsonl is left on disk
    warm.sync_ledger()
    assert_same(views(warm), before)
    warm.save_cache()
    warm.close()

    # Cold: no parse cache at all; the ledger alone carries the deleted history.
    cold = world.scanned(cache="fresh.pkl")
    assert_same(views(cold), before)
    assert len(cold.records) == 9  # nothing counted twice
    assert len({r.lkey for r in cold.records}) == 9
    cold.close()

    # And a warm restart after the rebuild still shows it (orphans are not cached).
    again = world.scanned()
    assert_same(views(again), before)


def test_history_is_visible_before_the_first_scan_completes_in_the_tui(world, tmp_path):
    """The TUI's warm path: history of deleted transcripts renders from the worker's
    pre-scan ledger load, and the app never double counts once the scan finishes."""
    from cc_usage.app import CCUsageApp

    world.scanned().close()
    delete_some(world)
    eng = world.engine()
    eng.refresh_limits = lambda: None
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert len(eng.records) == 9
            assert eng.snapshot(now=NOW).windows["all"].input_tokens == sum(
                r.input_tokens for r in eng.records
            )

    asyncio.run(scenario())
    eng.close()


# ── 2. truncated / rotated transcripts ───────────────────────────────────────────
def test_truncated_transcript_loses_nothing_and_counts_nothing_twice(world):
    eng = world.scanned()
    before = views(eng)
    s1 = world.alpha / "s1.jsonl"
    # Rewritten shorter: keeps only the last message and adds a brand-new one.
    s1.write_text(
        claude_line("r3", "m3", NOW - 2 * D, 50, 5, model="claude-mystery-1", cache_read=900)
        + claude_line("r7", "m7", NOW - 1 * H, 10, 1),
        "utf-8",
    )
    eng.scan()  # warm: re-read from the top; m3 folds into its record
    eng.sync_ledger()
    grown = views(eng)
    assert grown["records"] == before["records"] + 1
    assert grown["windows"]["all"][0] == before["windows"]["all"][0] + 10
    eng.save_cache()
    eng.close()

    cold = world.scanned(cache="fresh.pkl")  # m1/m2 are now only in the ledger
    assert_same(views(cold), grown)


def test_truncated_codex_rollout_is_not_counted_twice(world):
    eng = world.scanned()
    before = views(eng)
    rollout = world.sessions / ROLLOUT
    full = rollout.read_text("utf-8")
    first_two = "".join(full.splitlines(keepends=True)[:2])
    rollout.write_text(first_two, "utf-8")  # shorter -> re-read from the top
    eng.scan()
    eng.sync_ledger()
    assert_same(views(eng), before)  # the re-read events folded into their records
    rollout.write_text(full, "utf-8")  # and grown back to the full history
    eng.scan()
    eng.sync_ledger()
    assert_same(views(eng), before)
    assert len(ledger_rows(world.ledger)) == 9


def test_rotated_transcript_is_not_counted_twice(world):
    eng = world.scanned()
    before = views(eng)
    eng.close()
    (world.alpha / "s1.jsonl").rename(world.alpha / "s1-rotated.jsonl")
    again = world.scanned()  # the missing path discards the cache; keys are unchanged
    assert_same(views(again), before)
    assert len(ledger_rows(world.ledger)) == 9


# ── 3. streaming merge ───────────────────────────────────────────────────────────
def _ledger_output(world, mid_ts: float) -> int:
    rows = [row for row in ledger_rows(world.ledger) if row[3] == round(mid_ts * 1000)]
    assert len(rows) == 1
    return rows[0][6]


def test_streaming_final_counts_stored_in_the_same_scan(world):
    world.scanned()
    assert _ledger_output(world, NOW - 0.5 * H) == 500  # partial 5 and final 500 merged


def test_streaming_final_counts_stored_in_a_later_scan(world):
    s3 = world.alpha / "s3.jsonl"
    ts = NOW - 0.2 * H
    s3.write_text(claude_line("r9", "m9", ts, 900, 7, eph=(0, 0)), "utf-8")
    eng = world.scanned()
    assert _ledger_output(world, ts) == 7
    with s3.open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r9", "m9", ts + 5, 900, 1500, eph=(0, 0)))
    eng.scan()
    eng.sync_ledger()
    assert _ledger_output(world, ts) == 1500
    # Only the ledger remembers it now — with the final count.
    s3.unlink()
    cold = world.scanned(cache="fresh.pkl")
    [record] = [r for r in cold.records if r.input_tokens == 900]
    assert record.output_tokens == 1500
    assert record.lkey is not None and not cold.parser.has_key(record.lkey)


# ── 4. Codex keys ────────────────────────────────────────────────────────────────
def _codex_keys(parser: Parser) -> list[int]:
    return [r.lkey for r in parser.records if r.provider == CODEX_PROVIDER]


def test_codex_key_is_stable_across_reparse_archive_move_and_cold_rebuild(tmp_path):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    (active / ROLLOUT).write_text(World.rollout_text(), "utf-8")
    roots = [(active, "codex"), (archived, "codex")]
    cache = tmp_path / "cache.pkl"

    first = Parser(PRICING, cache_path=cache, roots=roots)
    first.scan()
    keys = _codex_keys(first)
    assert len(keys) == 3 and len(set(keys)) == 3  # re-emissions are not records
    first.save_cache()

    reparse = Parser(PRICING, roots=roots)  # re-parse from scratch
    reparse.scan()
    assert _codex_keys(reparse) == keys

    (active / ROLLOUT).rename(archived / ROLLOUT)  # Codex archives the rollout
    warm = Parser(PRICING, cache_path=cache, roots=roots)
    warm.scan()
    assert warm.stats.lines_read == 0  # the warm cache followed the move
    assert _codex_keys(warm) == keys

    cold = Parser(PRICING, roots=roots)  # cold rebuild from the archived location
    cold.scan()
    assert _codex_keys(cold) == keys

    # The key is the rollout's session, not its path: a copy under another root name
    # yields the same keys, so the parser folds it rather than double counting.
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    shutil.copy(archived / ROLLOUT, elsewhere / ROLLOUT)
    both = Parser(PRICING, roots=[(archived, "codex"), (elsewhere, "codex")])
    both.scan()
    assert _codex_keys(both) == keys


def test_codex_ledger_rows_survive_the_archive_move_without_duplicates(world):
    world.scanned().close()
    assert len(ledger_rows(world.ledger)) == 9
    (world.sessions / ROLLOUT).rename(world.archive / ROLLOUT)
    world.scanned().close()
    world.scanned(cache="fresh.pkl").close()  # cold rebuild from the archive
    assert len(ledger_rows(world.ledger)) == 9


def test_codex_unattributed_row_is_reattributed(world):
    rollout = world.sessions / ROLLOUT
    t0 = NOW - 5 * H
    rollout.write_text(codex_tokens(t0, (100, 20, 10), (100, 20, 10)), "utf-8")
    eng = world.scanned()
    [row] = [r for r in ledger_rows(world.ledger) if r[1] == CODEX_PROVIDER]
    assert row[4] == "codex-unattributed"

    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(codex_context(t0 + 1))
    eng.scan()
    eng.sync_ledger()
    [row] = [r for r in ledger_rows(world.ledger) if r[1] == CODEX_PROVIDER]
    assert row[4] == "gpt-test"  # the ledger followed the parser's re-attribution
    eng.close()

    rollout.unlink()
    cold = world.scanned(cache="fresh.pkl")
    [orphan] = [r for r in cold.records if r.provider == CODEX_PROVIDER]
    assert orphan.model_norm == "gpt-test" and orphan.known and orphan.cost > 0


# ── 5. pricing changes ───────────────────────────────────────────────────────────
def test_orphans_are_repriced_and_live_costs_are_bit_identical(world):
    world.scanned().close()
    delete_some(world)
    world.pricing = {
        **PRICING,
        "claude-opus-4-8": {"input": 7.0, "output": 31.0},
        "gpt-test": {"input": 3.0, "output": 9.0},
    }

    eng = world.scanned(cache="fresh.pkl")
    orphans = [r for r in eng.records if not eng.parser.has_key(r.lkey)]
    assert len(orphans) == 6
    for r in orphans:
        rates = get_rates(r.model_raw, world.pricing)
        expected = compute_cost(
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read=r.cache_read,
            cache_creation_total=r.cache_creation,
            ephemeral_5m=r._eph_5m,
            ephemeral_1h=r._eph_1h,
            rates=rates,
        )
        assert r.cost == expected  # priced with the NEW table, not the one it was seen under
    opus = [r for r in orphans if r.model_raw == "claude-opus-4-8"]
    assert opus and all(r.cost > 0 for r in opus)

    # Non-orphan rows: exactly what a ledger-less (main) parse computes.
    main = world.engine(cache=None, ledger=False)
    main.scan()
    by_key = {r.lkey: r.cost for r in main.parser.records}
    live = [r for r in eng.records if eng.parser.has_key(r.lkey)]
    assert len(live) == len(by_key) == 3
    for r in live:
        assert r.cost == by_key[r.lkey]


# ── 6. accounts: rename, disable, unconfigured ───────────────────────────────────
def _accounts(eng: Engine) -> dict[str, int]:
    return {a.label: a.input_tokens for a in eng.snapshot(now=NOW).accounts}


def test_label_rename_keeps_one_account(world):
    world.scanned().close()
    (world.beta / "s2.jsonl").unlink()
    renamed = Root("work", world.company, world.company / "projects", "config")
    world.claude_roots = [world.claude_roots[0], renamed]
    eng = world.scanned(cache="fresh.pkl")
    accounts = _accounts(eng)
    assert "company" not in accounts
    assert accounts["work"] == 2800  # both deleted messages, under the current label
    assert {r.account for r in eng.records if r.provider != CODEX_PROVIDER} == {"personal", "work"}


def test_disabled_root_rows_are_excluded(world):
    world.scanned().close()
    (world.beta / "s2.jsonl").unlink()
    world.claude_roots = [
        world.claude_roots[0],
        Root("company", world.company, world.company / "projects", "config", enabled=False),
    ]
    eng = world.scanned(cache="fresh.pkl")
    assert all(r.account != "company" for r in eng.records)
    assert "company" not in _accounts(eng)

    world.claude_roots = [
        world.claude_roots[0],
        Root("company", world.company, world.company / "projects", "config"),
    ]
    assert eng.reload_roots() is True
    eng.scan()
    eng.sync_ledger()
    assert _accounts(eng)["company"] == 2800  # re-enabled: its history is back


def test_unconfigured_root_falls_back_to_its_stored_label_without_merging(world):
    world.scanned().close()
    (world.beta / "s2.jsonl").unlink()
    # The company root is gone from config, and an unrelated root now uses its label.
    other = world.base / ".claude-other"
    (other / "projects" / "-x").mkdir(parents=True)
    (other / "projects" / "-x" / "s.jsonl").write_text(
        claude_line("r8", "m8", NOW - 2 * H, 5, 1), "utf-8"
    )
    world.claude_roots = [
        world.claude_roots[0],
        Root("company", other, other / "projects", "config"),
    ]
    eng = world.scanned(cache="fresh.pkl")
    accounts = _accounts(eng)
    assert accounts["company"] == 5  # the new root keeps its label and its own usage
    assert accounts["company-2"] == 2800  # the old root's history: stored label, suffixed


# ── 7. content-free ──────────────────────────────────────────────────────────────
def test_ledger_holds_no_transcript_content(world):
    marker = "ZQX-SECRET-7f3a"
    project = world.personal / "projects" / f"-home-{marker}-proj"
    project.mkdir()
    (project / f"{marker}.jsonl").write_text(
        claude_line(
            "r-m",
            "m-m",
            NOW - 1 * H,
            10,
            1,
            text=f"prompt {marker}",
            cwd=f"/w/{marker}",
            branch=marker,
        ),
        "utf-8",
    )
    with (world.sessions / ROLLOUT).open("a", encoding="utf-8") as fh:
        fh.write(codex_message(NOW - 1 * H, f"codex {marker}"))
        fh.write(codex_context(NOW - 1 * H, cwd=f"/w/{marker}"))
    eng = world.scanned()
    eng.close()
    blob = b"".join(
        p.read_bytes()
        for p in world.state.iterdir()
        if p.name.startswith("ledger.sqlite3")
    )
    assert len(ledger_rows(world.ledger)) == 10
    assert marker.encode() not in blob
    assert str(world.base).encode() not in blob  # not even the roots' paths


# ── 8. corrupt / busy / unavailable ledger ───────────────────────────────────────
def test_corrupt_ledger_is_moved_aside_and_rebuilt(world):
    garbage = b"this is not a sqlite database" * 64
    world.ledger.write_bytes(garbage)
    eng = world.scanned()
    moved = sorted(world.state.glob("ledger.sqlite3.corrupt-*"))
    assert len(moved) == 1 and moved[0].read_bytes() == garbage  # kept, never deleted
    assert len(ledger_rows(world.ledger)) == 9  # a fresh ledger from the transcripts
    warnings = eng.snapshot(now=NOW).warnings
    assert any(str(moved[0]) in w for w in warnings)


def test_corrupt_ledger_never_takes_the_panel_down(world):
    from textual.widgets import Static

    from cc_usage.app import CCUsageApp

    world.ledger.write_bytes(b"\x00garbage" * 512)
    eng = world.engine()
    eng.refresh_limits = lambda: None
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert len(eng.records) == 9
            notes = [t.plain for t in app.query_one("#notes", Static).renderable.renderables]
            assert any("corrupt-" in t and "moved it to" in t for t in notes)

    asyncio.run(scenario())
    eng.close()
    assert len(list(world.state.glob("ledger.sqlite3.corrupt-*"))) == 1


def test_unexpected_ledger_failure_leaves_the_panel_up(world, monkeypatch):
    from cc_usage.app import CCUsageApp

    eng = world.engine()
    eng.refresh_limits = lambda: None

    def boom(parser):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(eng, "_ledger_pass", boom)
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
            assert eng.is_scanned and len(eng.records) == 9
            assert any("ledger exploded" in w for w in eng.worker_warnings)

    asyncio.run(scenario())


def test_busy_ledger_retries_on_the_next_scan(world, monkeypatch):
    world.scanned().close()
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
    monkeypatch.setattr(ledger_module, "BUSY_TIMEOUT_MS", 50)
    blocker = sqlite3.connect(world.ledger, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")  # another ccusage mid-write
    try:
        eng = world.engine()
        eng.scan()
        eng.sync_ledger()
        assert any("busy" in w for w in eng.snapshot(now=NOW).warnings)
        assert len(eng.records) == 10  # the panel keeps its in-memory data
        assert eng.ledger_pending  # …and will retry
    finally:
        blocker.rollback()
        blocker.close()
    eng.sync_ledger()  # the next scan's sync
    assert len(ledger_rows(world.ledger)) == 10
    assert not any("ledger" in w for w in eng.snapshot(now=NOW).warnings)


def test_unwritable_ledger_runs_without_it_and_recovers(world, monkeypatch):
    real_write = ledger_module.Ledger.write

    def full(self, rows):
        raise LedgerUnavailable("database or disk is full")

    monkeypatch.setattr(ledger_module.Ledger, "write", full)
    eng = world.engine()
    eng.scan()
    eng.sync_ledger()
    before = views(eng)
    assert any("running without it" in w for w in eng.snapshot(now=NOW).warnings)
    eng.save_cache()  # the unstored records are saved as still pending…
    eng.close()

    monkeypatch.setattr(ledger_module.Ledger, "write", real_write)
    again = world.engine()
    assert again.prime_cache()
    again.sync_ledger()  # …and stored by the next run
    assert len(ledger_rows(world.ledger)) == 9
    assert_same(views(again), before)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions that apply to the test user",
)
def test_read_only_config_dir_runs_without_the_ledger(world):
    locked = world.base / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        eng = Engine(Config(), cache_path=world.state / "cache.pkl", ledger_path=locked / "l.db")
        eng.scan()
        eng.sync_ledger()  # must not raise
        warnings = eng.snapshot(now=NOW).warnings
        assert any("usage ledger unavailable" in w and "running without it" in w for w in warnings)
        assert len(eng.records) == 9 and eng.ledger_pending  # data intact; retried later
    finally:
        locked.chmod(0o755)
    assert not (locked / "l.db").exists()


def test_ledger_writes_only_happen_on_worker_threads(world, monkeypatch):
    """R8: the TUI writes the ledger from workers — the initial scan's and, after each
    UI-thread refresh scan, a dedicated ledger worker — never on the UI thread."""
    from cc_usage.app import CCUsageApp

    threads: list[bool] = []
    backups: list[bool] = []
    real_write = ledger_module.Ledger.write
    real_backup = ledger_module.Ledger.backup

    def spy(self, rows):
        threads.append(threading.current_thread() is threading.main_thread())
        return real_write(self, rows)

    def spy_backup(self):
        backups.append(threading.current_thread() is threading.main_thread())
        return real_backup(self)

    monkeypatch.setattr(ledger_module.Ledger, "write", spy)
    monkeypatch.setattr(ledger_module.Ledger, "backup", spy_backup)
    eng = world.engine()
    eng.refresh_limits = lambda: None
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
            app._refresh_data()  # the refresh timer's UI-thread scan
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())
    eng.close()
    assert len(threads) >= 2 and not any(threads)
    assert backups == [False]  # the daily backup, taken by a worker too
    assert len(ledger_rows(world.ledger)) == 10


# ── 9. concurrent writers ────────────────────────────────────────────────────────
def test_two_engines_writing_one_ledger_concurrently(world, tmp_path):
    engines = [world.engine(cache="a.pkl"), world.engine(cache="b.pkl")]
    errors: list[BaseException] = []
    s1 = world.alpha / "s1.jsonl"
    lock = threading.Lock()
    counter = iter(range(1000))

    def run(eng: Engine) -> None:
        try:
            eng.scan()
            for _ in range(15):
                with lock:
                    n = next(counter)
                    with s1.open("a", encoding="utf-8") as fh:
                        fh.write(claude_line(f"rx{n}", f"mx{n}", NOW - 1 * H, 1, 1))
                eng.scan()
                # Force the heavy path too: a full diff + backfill every other round.
                if n % 2:
                    eng._ledger_synced_parser = None
                eng.sync_ledger()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    workers = [threading.Thread(target=run, args=(eng,)) for eng in engines]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert not errors
    for eng in engines:
        eng.scan()
        eng.sync_ledger()
        assert not any("ledger" in w for w in eng.worker_warnings)
    conn = sqlite3.connect(world.ledger)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM usage").fetchone()[0] == 9 + 30
    finally:
        conn.close()
    for eng in engines:
        eng.close()


def test_two_processes_writing_one_ledger(world):
    """Real processes: each backfills its own root plus the shared one, repeatedly."""
    script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from pathlib import Path
from cc_usage.accounts import Root
from cc_usage.config import Config
import cc_usage.engine as E
root = Path(sys.argv[1]); name = sys.argv[2]
roots = [Root("personal", root / ".claude", root / ".claude" / "projects", "auto"),
         Root(name, root / name, root / name / "projects", "config")]
E.discover_claude_roots = lambda cfg: roots
E.discover_codex_roots = lambda *a, **k: []
E.load_pricing = lambda: ({{}}, [])
eng = E.Engine(Config(), cache_path=None, ledger_path=Path(sys.argv[3]))
eng.scan()
for _ in range(20):
    eng._ledger_synced_parser = None
    eng.parser.restore_dirty(eng.parser.live_index())
    eng.sync_ledger()
assert not eng.worker_warnings, eng.worker_warnings
eng.close()
"""
    for name, count in (("one", 40), ("two", 60)):
        project = world.base / name / "projects" / "-p"
        project.mkdir(parents=True)
        (project / "s.jsonl").write_text(
            "".join(claude_line(f"{name}{i}", f"{name}m{i}", NOW - i, 1, 1) for i in range(count)),
            "utf-8",
        )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(world.base), name, str(world.ledger)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("one", "two")
    ]
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode()
    conn = sqlite3.connect(world.ledger)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # personal's 4 messages (shared) + 40 + 60 of each process's own.
        assert conn.execute("SELECT count(*) FROM usage").fetchone()[0] == 4 + 40 + 60
    finally:
        conn.close()


# ── 10. no deletions: ledger on == ledger off ────────────────────────────────────
def test_ledger_on_and_off_are_identical_without_deletions(world):
    on = world.scanned()
    off = world.engine(cache=None, ledger=False)
    off.scan()
    assert on.records is on.parser.records  # no orphans: the live list, uncopied
    a, b = views(on), views(off)
    assert a == b  # exact, floats included
    assert len(ledger_rows(world.ledger)) == len(on.parser.records) == 9


def test_ledger_rows_match_the_live_records(world):
    eng = world.scanned()
    stored = {row[0]: row for row in ledger_rows(world.ledger)}
    for r in eng.parser.records:
        key, provider, label, ts, model, inp, outp, cr, cc, e5, e1 = stored[r.lkey]
        assert (provider, label, model) == (r.provider, r.account, r.model_raw)
        assert ts == round(r.ts * 1000) and ts / 1000 == r.ts
        assert (inp, outp, cr, cc, e5, e1) == (
            r.input_tokens,
            r.output_tokens,
            r.cache_read,
            r.cache_creation,
            r._eph_5m,
            r._eph_1h,
        )
    # The NULL-vs-0 sub-bucket distinction survives (m2 had no cache_creation object).
    assert any(row[9] is None for row in stored.values())
    assert any(row[9] == 0 for row in stored.values())


# ── 11. --ledger-info ────────────────────────────────────────────────────────────
@pytest.fixture
def info(world, monkeypatch, capsys):
    real = engine_module.Engine
    monkeypatch.setattr(
        engine_module, "Engine", lambda cfg: real(cfg, cache_path=world.state / "cache.pkl")
    )
    monkeypatch.setattr("cc_usage.cli.load_config", lambda: Config())

    def run() -> tuple[int, str]:
        code = cli_main(["--ledger-info"])
        return code, capsys.readouterr().out

    return run


def test_ledger_info_before_any_ledger(world, info):
    code, out = info()
    assert code == 0
    assert str(world.ledger) in out and "no ledger yet" in out
    assert not world.ledger.exists()  # read-only: it did not create one


def test_ledger_info_reports_rows_range_and_orphans(world, info):
    world.scanned().close()
    delete_some(world)
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))  # parsed, not yet recorded
    stamp = {p.name: p.stat().st_mtime_ns for p in world.state.iterdir()}
    snapshot = {p.name: p.read_bytes() for p in world.state.iterdir()}

    code, out = info()
    assert code == 0
    assert str(world.ledger) in out
    assert "records     9  (claude 6 · codex 3)" in out
    assert "personal 4" in out and "company 2" in out and "codex 3" in out
    first = datetime.datetime.fromtimestamp(NOW - 20 * D).date().isoformat()
    last = datetime.datetime.fromtimestamp(NOW - 0.5 * H).date().isoformat()
    assert f"covers      {first} → {last}" in out
    assert "orphans     6 " in out  # 3 deleted Claude messages + 3 Codex events
    assert "unrecorded  1 " in out
    assert "before you shorten Claude Code's transcript retention" in " ".join(out.split())
    # Read-only: the ledger and the parse cache are untouched. A read-only SQLite
    # connection may create its own -wal/-shm index files; the WAL stays empty.
    after = {p.name: p for p in world.state.iterdir()}
    for name, content in snapshot.items():
        assert after[name].read_bytes() == content, name
        assert after[name].stat().st_mtime_ns == stamp[name], name
    assert set(after) - set(snapshot) <= {"ledger.sqlite3-wal", "ledger.sqlite3-shm"}
    if "ledger.sqlite3-wal" in after and "ledger.sqlite3-wal" not in snapshot:
        assert after["ledger.sqlite3-wal"].stat().st_size == 0


def test_ledger_info_all_recorded(world, info):
    world.scanned().close()
    code, out = info()
    assert code == 0
    assert "orphans     0 " in out and "unrecorded  0 " in out
    assert "Every parsed usage record from every enabled root is in the ledger" in " ".join(
        out.split()
    )
    assert "disabled" not in out


def test_ledger_info_on_a_corrupt_ledger_moves_nothing(world, info):
    world.ledger.write_bytes(b"not a database" * 100)
    code, out = info()
    assert code == 1 and "unreadable" in out
    assert world.ledger.read_bytes() == b"not a database" * 100
    assert not list(world.state.glob("ledger.sqlite3.corrupt-*"))


# ── critic round 1 ───────────────────────────────────────────────────────────────
def _synthetic_rows(world: World, n: int, *, start: int = 0) -> list:
    """History rows for the personal root that no transcript on disk still holds."""
    from cc_usage.accounts import root_identity
    from cc_usage.ledger import LedgerRow
    from cc_usage.parser import ledger_key

    identity = root_identity(world.personal)
    return [
        LedgerRow(
            ledger_key(f"synthetic-{i}"),
            "claude",
            identity,
            "personal",
            round((NOW - 40 * D + i) * 1000),
            "claude-opus-4-8",
            100 + i % 7,
            10 + i % 5,
            1000,
            0,
            None,
            None,
        )
        for i in range(start, start + n)
    ]


def _usage_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM usage").fetchone()[0]
    finally:
        conn.close()


def _corrupt_a_usage_leaf(path: Path) -> None:
    """Scribble over one leaf page of the usage table (not its root, not page 1)."""
    conn = sqlite3.connect(path)
    try:
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'usage'").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()
    target = pages if pages != root else pages - 1
    with open(path, "r+b") as fh:
        fh.seek((target - 1) * page_size)
        fh.write(b"\xa5" * page_size)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT sum(inp) FROM usage").fetchone()
    finally:
        conn.close()


# 1. a live record never shows less than the ledger stored for it
def test_resumed_copy_with_lower_counters_keeps_the_stored_max(world):
    """A resumed session copies a message into a newer file with zeroed counters.
    Once retention deletes the original, the live record must not drop to the copy's
    values — warm or cold."""
    resumed = world.alpha / "s9-resumed.jsonl"
    resumed.write_text(
        claude_line("r4", "m4", NOW - 20 * D, 0, 0) + claude_line("r7", "m7", NOW - 1 * H, 10, 1),
        "utf-8",
    )
    first = world.scanned()
    before = views(first)
    first.close()
    (world.alpha / "sub" / "agent-1.jsonl").unlink()  # the original, with the real m4

    warm = world.scanned()
    assert_same(views(warm), before)
    warm.close()
    cold = world.scanned(cache="fresh.pkl")
    assert_same(views(cold), before)
    [m4] = [r for r in cold.records if r.input_tokens == 700]
    assert cold.parser.has_key(m4.lkey)  # still live (from the copy), at the stored max
    assert m4.output_tokens == 70 and m4.cache_read == 5000
    # And the ledger was never lowered by the zeroed copy.
    assert [row for row in ledger_rows(world.ledger) if row[0] == m4.lkey][0][5:8] == (700, 70, 5000)


def test_cut_back_streaming_reply_keeps_its_final_count_after_a_cold_rebuild(world):
    s3 = world.alpha / "s3.jsonl"
    partial = claude_line("r9", "m9", NOW - 0.2 * H, 900, 5)
    s3.write_text(partial + claude_line("r9", "m9", NOW - 0.2 * H, 900, 357), "utf-8")
    first = world.scanned()
    before = views(first)
    first.close()
    s3.write_text(partial, "utf-8")  # cut back to the partial line
    cold = world.scanned(cache="fresh.pkl")
    assert_same(views(cold), before)
    [m9] = [r for r in cold.records if r.input_tokens == 900]
    assert m9.output_tokens == 357


# 2. the key scheme is recorded, migrated exactly once, and never guessed at
def _meta_scheme(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT v FROM meta WHERE k = 'key_scheme'").fetchone()[0])
    finally:
        conn.close()


def test_ledger_records_its_key_scheme(world):
    from cc_usage.parser import KEY_SCHEME

    world.scanned().close()
    assert _meta_scheme(world.ledger) == KEY_SCHEME
    conn = sqlite3.connect(world.ledger)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger_module.SCHEMA_VERSION
    finally:
        conn.close()


def test_key_scheme_migration_runs_once(world, monkeypatch):
    world.scanned().close()
    current = ledger_module.KEY_SCHEME
    calls = []

    def migrate(conn):
        calls.append(1)
        conn.execute("DELETE FROM usage WHERE outp = 70")  # e.g. a record now dropped

    monkeypatch.setattr(ledger_module, "KEY_SCHEME", current + 1)
    monkeypatch.setitem(ledger_module.KEY_SCHEME_MIGRATIONS, current, migrate)
    for _ in range(2):
        eng = world.scanned()
        assert not any("ledger" in w for w in eng.worker_warnings)
        eng.close()
    assert calls == [1]
    assert _meta_scheme(world.ledger) == current + 1


def test_ledger_without_a_migration_path_is_refused_untouched(world, monkeypatch):
    world.scanned().close()
    before = _usage_count(world.ledger)
    monkeypatch.setattr(ledger_module, "KEY_SCHEME", ledger_module.KEY_SCHEME + 5)
    eng = world.scanned()
    assert any("no migration" in w and "running without it" in w for w in eng.worker_warnings)
    assert len(eng.records) == 9  # the panel keeps its live data
    eng.close()
    assert _usage_count(world.ledger) == before
    assert _meta_scheme(world.ledger) == ledger_module.KEY_SCHEME - 5


def test_ledger_from_a_newer_scheme_is_left_untouched(world):
    world.scanned().close()
    conn = sqlite3.connect(world.ledger)
    conn.execute("UPDATE meta SET v = '99' WHERE k = 'key_scheme'")
    conn.commit()
    conn.close()
    eng = world.scanned()
    assert any("newer than this ccusage" in w for w in eng.worker_warnings)
    eng.close()
    assert _meta_scheme(world.ledger) == 99


def test_prerelease_ledger_is_migrated_without_double_counting(world):
    """A ledger from the pre-release build (schema v1, no key scheme) keyed Codex
    events differently: its Codex rows are dropped and re-recorded, Claude rows kept."""
    eng = world.scanned()
    before = views(eng)
    eng.close()
    conn = sqlite3.connect(world.ledger)
    conn.execute("DROP TABLE meta")
    conn.execute("PRAGMA user_version = 1")
    acct = conn.execute("SELECT id FROM accounts WHERE provider = 'codex'").fetchone()[0]
    model = conn.execute("SELECT id FROM models LIMIT 1").fetchone()[0]
    conn.execute(  # an old-scheme Codex key that no longer matches any live event
        "INSERT INTO usage VALUES (12345, ?, ?, ?, 777, 7, 0, 0, 0, 0)",
        (acct, round((NOW - 5 * H) * 1000), model),
    )
    conn.commit()
    conn.close()
    (world.alpha / "sub" / "agent-1.jsonl").unlink()  # Claude history kept across it
    again = world.scanned(cache="fresh.pkl")
    assert_same(views(again), before)
    assert 12345 not in {row[0] for row in ledger_rows(world.ledger)}
    assert _meta_scheme(world.ledger) == ledger_module.KEY_SCHEME


# 4. a lone surrogate in a transcript never stops the scan or the ledger
def test_lone_surrogates_do_not_stop_the_scan_or_the_ledger(world):
    weird = world.alpha / "weird.jsonl"
    weird.write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "req-\ud800",
                "timestamp": _iso(NOW - 2 * H),
                "message": {
                    "id": "msg-\udfff",
                    "model": "claude-\ud800-odd",
                    "usage": {"input_tokens": 11, "output_tokens": 2},
                },
            }
        )
        + "\n"
        + claude_line("r8", "m8", NOW - 1 * H, 12, 3),
        "utf-8",
    )
    eng = world.scanned()
    eng.scan()  # the next tick too
    assert {r.input_tokens for r in eng.parser.records} >= {11, 12}
    assert not any("ledger" in w for w in eng.worker_warnings)
    assert len(ledger_rows(world.ledger)) == 11


# 5. an unreadable ledger is salvaged, and the daily backup fills what is not
def test_partially_damaged_ledger_is_salvaged_row_by_row(tmp_path):
    from cc_usage.ledger import Ledger, LedgerRow
    from cc_usage.parser import ledger_key

    path = tmp_path / "ledger.sqlite3"
    rows = [
        LedgerRow(ledger_key(f"k{i}"), "claude", "id", "personal", 1_780_000_000_000 + i,
                  "claude-opus-4-8", i, 1, 2, 3, None, 0)
        for i in range(5000)
    ]
    led = Ledger(path)
    led.write(rows)
    led.close()
    _corrupt_a_usage_leaf(path)

    led = Ledger(path)
    moved = led.move_aside()
    report = led.recover(moved)
    led.close()
    assert not report.salvage_complete and report.from_backup == 0
    assert 5000 - 200 < report.salvaged < 5000  # only the scribbled page is lost
    assert _usage_count(path) == report.salvaged
    assert "no backup" in report.describe()
    stored = {row[0]: row for row in ledger_rows(path)}
    original = {r.key: r for r in rows}
    assert all(original[k].inp == row[5] for k, row in stored.items())  # values intact


def test_damaged_ledger_is_restored_from_the_backup(world):
    """Deleted-transcript history lives only in the ledger; a damaged ledger must not
    take it down with it."""
    first = world.scanned()
    first._ledger.write(_synthetic_rows(world, 3000))  # history of deleted transcripts
    first._ledger_synced_parser = None
    first.sync_ledger()
    before = views(first)
    assert before["records"] == 9 + 3000
    first._ledger.backup()
    first.close()
    _corrupt_a_usage_leaf(world.ledger)

    eng = world.scanned(cache="fresh.pkl")
    assert_same(views(eng), before)  # nothing lost
    moved = list(world.state.glob("ledger.sqlite3.corrupt-*"))
    assert len(moved) == 1
    [note] = [w for w in eng.worker_warnings if "damaged" in w]
    assert str(moved[0]) in note and "from the backup of" in note
    assert _usage_count(world.ledger) == 9 + 3000

    # Even a ledger that is garbage from its first byte comes back from the backup.
    eng.close()
    world.ledger.write_bytes(b"\x00" * 8192)
    again = world.scanned(cache="fresh2.pkl")
    assert_same(views(again), before)


def test_backup_is_daily_and_verified(world):
    eng = world.scanned()
    bak = world.state / "ledger.sqlite3.bak"
    assert bak.exists() and _usage_count(bak) == 9
    stamp = bak.stat().st_mtime_ns
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
    eng.scan()
    eng.sync_ledger()
    assert bak.stat().st_mtime_ns == stamp  # not again within the day
    old = bak.stat().st_mtime - ledger_module.BACKUP_INTERVAL_SECS - 1
    os.utime(bak, (old, old))
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r8", "m8", NOW - 1 * H, 10, 1))
    eng.scan()
    eng.sync_ledger()
    assert _usage_count(bak) == 11  # refreshed once a day had passed
    # A copy that fails its integrity check never replaces a good backup.
    eng._ledger.write(_synthetic_rows(world, 3000))
    eng.close()
    good = bak.read_bytes()
    _corrupt_a_usage_leaf(world.ledger)
    with pytest.raises(ledger_module.LedgerCorrupt):
        ledger_module.Ledger(world.ledger).backup()
    assert bak.read_bytes() == good
    assert not list(world.state.glob("*.tmp"))


# 6. a second process follows the ledger to its new file
def test_second_process_reopens_after_the_ledger_is_moved_aside(world):
    from cc_usage.ledger import Ledger

    world.scanned().close()
    a, b = Ledger(world.ledger), Ledger(world.ledger)
    b.write(_synthetic_rows(world, 1))  # b holds an open handle
    moved = a.move_aside()
    a.recover(moved)
    a.close()
    assert b.ensure_current() is True  # noticed the swap
    b.write(_synthetic_rows(world, 1, start=100))
    b.close()
    assert _usage_count(moved) == 10  # nothing more went into the moved-aside file
    assert _usage_count(world.ledger) == 11


def test_engine_resyncs_everything_into_a_replaced_ledger(world):
    eng = world.scanned()
    other = ledger_module.Ledger(world.ledger)
    moved = other.move_aside()
    other.close()
    assert moved is not None
    world.ledger.unlink(missing_ok=True)
    eng.sync_ledger()  # its old handle points at the moved file; it must follow
    assert _usage_count(world.ledger) == 9
    eng.close()


# 7. --ledger-info
def test_ledger_info_reads_an_uncheckpointed_wal(world, info):
    world.scanned().close()
    writer = ledger_module.Ledger(world.ledger)
    writer._open().execute("PRAGMA wal_autocheckpoint = 0")
    writer.write(_synthetic_rows(world, 25))
    assert (world.state / "ledger.sqlite3-wal").stat().st_size > 0
    try:
        code, out = info()
    finally:
        writer.close()
    assert code == 0
    assert "records     34  (claude 31 · codex 3)" in out
    assert "orphans     25 " in out


def test_ledger_info_names_disabled_roots_and_never_calls_it_safe(world, info):
    world.scanned().close()
    world.claude_roots = [
        world.claude_roots[0],
        Root("company", world.company, world.company / "projects", "config", enabled=False),
    ]
    code, out = info()
    flat = " ".join(out.split())
    assert code == 0
    assert f"disabled company ({world.company}): not scanned" in flat
    assert "company 2 (disabled)" in flat
    assert "Every parsed usage record" not in flat
    assert "Disabled roots are not recorded (company)" in flat
    assert "before you shorten Claude Code's transcript retention" in flat


# 8. the view can never count a record both live and from the ledger
def test_parser_registers_a_key_before_listing_the_record(world):
    parser = Parser(PRICING)
    seen: list[bool] = []

    class Watched(list):
        def append(self, record):
            seen.append(parser.has_key(record.lkey))
            super().append(record)

    parser.records = Watched()
    parser.ingest_file(world.alpha / "s1.jsonl")
    assert seen and all(seen)


def test_view_does_not_double_count_a_record_appended_mid_read(world, monkeypatch):
    world.scanned().close()
    (world.alpha / "sub" / "agent-1.jsonl").unlink()
    eng = world.scanned(cache="fresh.pkl")
    [orphan] = [r for r in eng.records if not eng.parser.has_key(r.lkey)][:1]
    revived = copy.copy(orphan)
    parser = eng.parser
    real = parser.has_key
    fired = []

    def racing(key):
        answer = real(key)
        if key == orphan.lkey and not fired:  # a scan lands right after this check
            fired.append(1)
            parser._by_key[key] = revived
            parser.records.append(revived)
        return answer

    monkeypatch.setattr(parser, "has_key", racing)
    eng._view = None
    records = eng.records
    assert fired
    assert sum(1 for r in records if r.lkey == orphan.lkey) <= 1


# the critic's mutation list
def test_revived_transcript_is_not_counted_twice(world):
    agent = world.alpha / "sub" / "agent-1.jsonl"
    text = agent.read_text("utf-8")
    eng = world.scanned()
    before = views(eng)
    eng.close()
    agent.unlink()
    eng = world.scanned(cache="fresh.pkl")
    assert len(eng._orphans) == 1
    agent.write_text(text, "utf-8")  # the transcript comes back (restored, re-synced…)
    eng.scan()
    assert_same(views(eng), before)  # at once, before any ledger worker runs
    eng.sync_ledger()
    assert eng._orphans == []  # and the worker prunes it
    assert_same(views(eng), before)


def test_view_includes_records_appended_after_it_was_cached(world):
    world.scanned().close()
    delete_some(world)
    eng = world.scanned(cache="fresh.pkl")
    assert eng._orphans
    before = views(eng)
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
    eng.scan()
    after = views(eng)
    assert after["records"] == before["records"] + 1
    assert after["windows"]["all"][0] == before["windows"]["all"][0] + 10


def test_upsert_never_lowers_a_stored_value(tmp_path):
    from cc_usage.ledger import Ledger, LedgerRow

    led = Ledger(tmp_path / "l.sqlite3")
    high = LedgerRow(1, "claude", "i", "p", 1000, "m", 700, 70, 5000, 40, 30, 10)
    low = LedgerRow(1, "claude", "i", "p", 1000, "m", 0, 0, 0, 0, 0, 0)
    led.write([high])
    led.write([low])
    assert led.rows([1])[0][4:] == (700, 70, 5000, 40, 30, 10)
    led.write([LedgerRow(1, "claude", "i", "p", 1000, "m", 800, 60, 5000, 40, 30, 10)])
    assert led.rows([1])[0][4:] == (800, 70, 5000, 40, 30, 10)  # field-wise max
    led.close()


def test_subbucket_null_and_zero_merge_like_the_parser(tmp_path):
    from cc_usage.ledger import Ledger, LedgerRow

    led = Ledger(tmp_path / "l.sqlite3")

    def row(key, e5, e1):
        return LedgerRow(key, "claude", "i", "p", 1000, "m", 10, 1, 0, 400, e5, e1)

    def grown(key, e5, e1):  # a later streaming line: more output, so the row updates
        return LedgerRow(key, "claude", "i", "p", 1000, "m", 10, 99, 0, 400, e5, e1)

    led.write([row(1, None, None), row(2, 300, 0), row(3, None, None), row(4, 500, 20)])
    led.write([grown(1, 300, 100), grown(2, None, None), grown(3, None, None), grown(4, 200, 90)])
    stored = {r[0]: r[8:] for r in led.rows([1, 2, 3, 4])}
    # NULL = "no sub-bucket object": kept only while both sides lack it; else the max.
    assert stored == {1: (300, 100), 2: (300, 0), 3: (None, None), 4: (500, 90)}
    led.close()


def test_failed_incremental_write_is_retried_on_the_next_sync(world, monkeypatch):
    eng = world.scanned()  # the full diff is done; later syncs are incremental
    with (world.alpha / "s1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
    eng.scan()
    real_write = ledger_module.Ledger.write

    def busy(self, rows):
        raise ledger_module.LedgerBusy("database is locked")

    monkeypatch.setattr(ledger_module.Ledger, "write", busy)
    eng.sync_ledger()
    monkeypatch.setattr(ledger_module.Ledger, "write", real_write)
    eng.sync_ledger()
    assert len(ledger_rows(world.ledger)) == 10


def test_unstored_record_survives_a_restart_after_its_transcript_is_deleted(
    world, monkeypatch
):
    s3 = world.alpha / "s3.jsonl"
    s3.write_text(claude_line("r1x", "m1x", NOW - 3 * D, 1, 1), "utf-8")
    eng = world.scanned()
    with s3.open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r7", "m7", NOW - 1 * H, 10, 1))
    eng.scan()
    real_write = ledger_module.Ledger.write

    def full(self, rows):
        raise LedgerUnavailable("database or disk is full")

    monkeypatch.setattr(ledger_module.Ledger, "write", full)
    eng.sync_ledger()
    eng.save_cache()  # m7 is saved as not yet in the ledger
    eng.close()
    monkeypatch.setattr(ledger_module.Ledger, "write", real_write)
    s3.unlink()  # …and its transcript is deleted before the next run

    again = world.engine()
    again.scan()  # like `--once`: the cache no longer matches disk and is rebuilt
    again.sync_ledger()
    assert any(r.input_tokens == 10 and r.output_tokens == 1 for r in again.records)
    assert len(ledger_rows(world.ledger)) == 11


def test_damaged_ledger_moves_its_wal_and_shm_aside_too(world):
    world.ledger.write_bytes(b"not a database" * 300)
    wal = world.state / "ledger.sqlite3-wal"
    wal.write_bytes(b"old wal frames" * 10)
    shm = world.state / "ledger.sqlite3-shm"
    shm.write_bytes(b"\x00" * 64)
    world.scanned().close()
    [moved] = [p for p in world.state.glob("ledger.sqlite3.corrupt-*") if p.suffix != ".bak"
               and not p.name.endswith(("-wal", "-shm"))]
    assert Path(f"{moved}-wal").read_bytes() == b"old wal frames" * 10
    assert Path(f"{moved}-shm").exists()
    assert _usage_count(world.ledger) == 9


def test_ledger_restored_from_an_older_backup_catches_up_with_the_parse(world):
    """The backup predates a record's final streaming line. After recovery the ledger
    must be raised to what the (warm) parse holds, not left at the backup's value."""
    s3 = world.alpha / "s3.jsonl"
    ts = NOW - 0.2 * H
    s3.write_text(claude_line("r9", "m9", ts, 900, 7), "utf-8")
    eng = world.scanned()  # the first sync also takes the daily backup (out = 7)
    with s3.open("a", encoding="utf-8") as fh:
        fh.write(claude_line("r9", "m9", ts + 5, 900, 1500))
    eng.scan()
    eng.sync_ledger()
    eng.save_cache()
    eng.close()
    world.ledger.write_bytes(b"\x00" * 4096)  # nothing salvageable

    warm = world.engine()
    assert warm.prime_cache()  # the record is not dirty: it comes from the cache
    warm.sync_ledger()
    warm.close()
    assert _ledger_output(world, ts) == 1500
    s3.unlink()
    cold = world.scanned(cache="fresh.pkl")
    assert [r.output_tokens for r in cold.records if r.input_tokens == 900] == [1500]


def test_backup_that_fails_its_check_never_replaces_the_good_one(world, monkeypatch):
    eng = world.scanned()
    bak = world.state / "ledger.sqlite3.bak"
    good = bak.read_bytes()
    real_connect = ledger_module.sqlite3.connect

    class Checked(sqlite3.Connection):
        def execute(self, sql, *args):
            if "quick_check" in sql:  # the copy reports damage instead of "ok"
                return super().execute("SELECT 'row 3 missing from index'")
            return super().execute(sql, *args)

    def connect(target, *args, **kwargs):
        if str(target).endswith(".tmp"):
            kwargs["factory"] = Checked
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", connect)
    with pytest.raises(ledger_module.LedgerCorrupt, match="integrity check"):
        eng._ledger.backup()
    assert bak.read_bytes() == good
    assert not list(world.state.glob("*.tmp"))
    eng.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_warm_start_backfills_a_lost_ledger(world, damage):
    world.scanned().close()
    for side in world.state.glob("ledger.sqlite3*"):
        side.unlink()  # the ledger and its backup are gone
    if damage == "corrupt":
        world.ledger.write_bytes(b"garbage" * 1000)
    eng = world.engine()
    assert eng.prime_cache()  # warm: nothing is re-parsed, so nothing is "dirty"
    eng.sync_ledger()
    assert len(ledger_rows(world.ledger)) == 9
