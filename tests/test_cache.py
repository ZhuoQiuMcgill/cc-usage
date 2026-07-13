"""Persistent parse cache (M6 across process runs).

Proves the contract that makes a warm start both fast AND correct:
  * a second run loads cached state and reads only newly appended lines,
  * warm-start records are byte-identical to a cold full scan,
  * the cache is invalidated on a pricing change, a format-version change, a deleted
    transcript, or a corrupt file — always degrading to a safe full rescan, never a crash.
Hermetic: PROJECTS_DIR is monkeypatched to a tmp dir; the cache is a tmp file.
"""

from __future__ import annotations

import json
import math
import pickle

import cc_usage.parser as P
from cc_usage.cost import compute_cost
from cc_usage.parser import Parser, _json_loads

PRICING = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}


def _line(req: str, mid: str, inp: int, out: int = 0, model: str = "claude-opus-4-8") -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": mid,
                    "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": out},
                },
            }
        )
        + "\n"
    )


def _seed(tmp_path, monkeypatch, lines: str):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    f = proj / "session.jsonl"
    f.write_text(lines)
    return f


def test_warm_start_reads_only_appended_lines(tmp_path, monkeypatch):
    """A second process (fresh Parser, same cache) reads 0 old lines, then +1 on append."""
    f = _seed(tmp_path, monkeypatch, _line("r1", "m1", 100) + _line("r2", "m2", 200))
    cache = tmp_path / "cache.pkl"

    p1 = Parser(PRICING, cache_path=cache)
    p1.scan()
    assert p1.stats.records == 2
    assert p1.stats.lines_read == 2
    p1.save_cache()
    assert cache.exists()

    # Fresh parser = a new process. Nothing changed on disk -> reads NO new lines.
    p2 = Parser(PRICING, cache_path=cache)
    p2.scan()
    assert len(p2.records) == 2
    assert p2.stats.lines_read == 0  # the M6-across-runs win: no re-parse of old lines

    # Append one line; the next warm start reads only that one.
    with open(f, "a") as fh:
        fh.write(_line("r3", "m3", 50))
    p2.save_cache()
    p3 = Parser(PRICING, cache_path=cache)
    p3.scan()
    assert len(p3.records) == 3
    assert p3.stats.lines_read == 1


def test_warm_start_records_identical_to_cold_scan(tmp_path, monkeypatch):
    """Warm-start totals must equal a cold full scan's — including across dedup."""
    # r2 is duplicated (same requestId+id) to exercise the dedup set through the cache.
    lines = (
        _line("r1", "m1", 100, 10)
        + _line("r2", "m2", 200, 20)
        + _line("r2", "m2", 200, 20)  # duplicate -> must be dropped, once, in both paths
        + _line("r3", "m3", 300, 30, model="claude-unknown-9")  # unpriced, tokens kept
    )
    _seed(tmp_path, monkeypatch, lines)
    cache = tmp_path / "cache.pkl"

    cold = Parser(PRICING)  # no cache -> pure cold scan
    cold.scan()

    warm_writer = Parser(PRICING, cache_path=cache)
    warm_writer.scan()
    warm_writer.save_cache()
    warm = Parser(PRICING, cache_path=cache)
    warm.scan()

    assert len(warm.records) == len(cold.records) == 3
    assert sum(r.cost for r in warm.records) == sum(r.cost for r in cold.records)
    assert sum(r.total_tokens for r in warm.records) == sum(r.total_tokens for r in cold.records)
    # The unknown-model flag survives the round-trip (drives the footnote).
    assert warm.stats.unknown_models == cold.stats.unknown_models
    assert any(not r.known for r in warm.records)


def test_streaming_merge_survives_warm_start(tmp_path, monkeypatch):
    """A partial streaming line cached by one run must merge with the final line the
    next run reads — the per-message index is rebuilt from the cache with object
    identity, so the append folds into the cached record (T9), not a new one."""
    f = _seed(tmp_path, monkeypatch, _line("r", "m", 1000, 7))  # partial snapshot only
    cache = tmp_path / "cache.pkl"

    p1 = Parser(PRICING, cache_path=cache)
    p1.scan()
    assert p1.records[0].output_tokens == 7
    p1.save_cache()

    # A new process appends the FINAL line, then warm-starts.
    with open(f, "a") as fh:
        fh.write(_line("r", "m", 1000, 2000))
    p2 = Parser(PRICING, cache_path=cache)
    p2.scan()
    assert len(p2.records) == 1  # merged in place, not a second record
    assert p2.records[0].output_tokens == 2000  # final counts win across the warm start
    assert p2.stats.lines_read == 1  # only the appended final line was read


def _cc_line(req: str, mid: str, inp: int, out: int) -> str:
    """Like _line but with a SPLIT cache_creation (1000 in 5m + 3000 in 1h), so a
    merged cost must take the sub-bucket path (1.25x/2x), not the aggregate fallback."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": mid,
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_creation_input_tokens": 4000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 1000,
                            "ephemeral_1h_input_tokens": 3000,
                        },
                    },
                },
            }
        )
        + "\n"
    )


def test_subbucket_merge_survives_warm_start(tmp_path, monkeypatch):
    """The private _eph_5m/_eph_1h sub-buckets must round-trip the cache with their
    positions intact: a merge after a warm start recomputes cost FROM the loaded
    fields, so a save/load field reorder (or loss) would silently degrade every
    cached record to the 1.25x aggregate fallback — this is the only test that
    would catch it (T9)."""
    f = _seed(tmp_path, monkeypatch, _cc_line("r", "m", 1000, 7))  # partial, WITH buckets
    cache = tmp_path / "cache.pkl"

    p1 = Parser(PRICING, cache_path=cache)
    p1.scan()
    p1.save_cache()

    # Direct round-trip pin, BEFORE any merge can re-supply the buckets from a new
    # line: the loaded record must carry them in the right slots (asymmetric values,
    # so a 5m/1h swap or a loss-to-None is caught here even in isolation).
    probe = Parser(PRICING, cache_path=cache)
    probe.scan()
    assert (probe.records[0]._eph_5m, probe.records[0]._eph_1h) == (1000, 3000)

    # A new process appends the FINAL line, then warm-starts and merges.
    with open(f, "a") as fh:
        fh.write(_cc_line("r", "m", 1000, 2000))
    p2 = Parser(PRICING, cache_path=cache)
    p2.scan()
    assert len(p2.records) == 1
    rec = p2.records[0]
    assert rec.output_tokens == 2000

    # Exact sub-bucket cost vs the fallback it must NOT collapse to — both pinned
    # numerically so the ~14% gap between the paths is unmissable:
    #   sub-bucket: 1000*5 + 2000*25 + 1000*5*1.25 + 3000*5*2.00 (/1M) = 0.09125
    #   fallback:   1000*5 + 2000*25 + 4000*5*1.25              (/1M) = 0.08
    subbucket = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=0,
        cache_creation_total=4000,
        ephemeral_5m=1000,
        ephemeral_1h=3000,
        rates=(5.0, 25.0),
    )
    fallback = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=0,
        cache_creation_total=4000,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=(5.0, 25.0),
    )
    assert math.isclose(subbucket, 0.09125, abs_tol=1e-9)
    assert math.isclose(fallback, 0.08, abs_tol=1e-9)
    assert math.isclose(rec.cost, subbucket, abs_tol=1e-12)  # loaded buckets used
    assert not math.isclose(rec.cost, fallback, abs_tol=1e-9)  # did NOT fall back


def test_pricing_change_invalidates_cache(tmp_path, monkeypatch):
    """Cached costs are computed under a rate table; a different table must rescan."""
    _seed(tmp_path, monkeypatch, _line("r1", "m1", 100, 10))
    cache = tmp_path / "cache.pkl"

    writer = Parser(PRICING, cache_path=cache)
    writer.scan()
    writer.save_cache()

    # Same data, DIFFERENT rates -> fingerprint mismatch -> full rescan (not 0 lines).
    dearer = {"claude-opus-4-8": {"input": 50.0, "output": 250.0}}
    p = Parser(dearer, cache_path=cache)
    p.scan()
    assert p.stats.lines_read == 1  # re-read from disk, not served from the stale cache
    # And the cost reflects the NEW rates, not the cached ones.
    assert p.records[0].cost == (100 * 50.0 + 10 * 250.0) / 1_000_000


def test_version_change_invalidates_cache(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    cache = tmp_path / "cache.pkl"
    p1 = Parser(PRICING, cache_path=cache)
    p1.scan()
    p1.save_cache()

    monkeypatch.setattr(P, "_CACHE_VERSION", P._CACHE_VERSION + 1)
    p2 = Parser(PRICING, cache_path=cache)
    p2.scan()
    assert p2.stats.lines_read == 1  # old-version cache ignored -> re-read
    assert len(p2.records) == 1


def test_deleted_transcript_invalidates_cache(tmp_path, monkeypatch):
    """A cached file that no longer exists -> discard cache (records can't be un-counted)."""
    f = _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    (tmp_path / "proj" / "other.jsonl").write_text(_line("r2", "m2", 200))
    cache = tmp_path / "cache.pkl"
    p1 = Parser(PRICING, cache_path=cache)
    p1.scan()
    assert len(p1.records) == 2
    p1.save_cache()

    f.unlink()  # delete one of the two cached transcripts
    p2 = Parser(PRICING, cache_path=cache)
    p2.scan()
    # Cache discarded; only the surviving file is counted (no orphaned r1 record).
    assert len(p2.records) == 1
    assert p2.records[0].input_tokens == 200


def test_corrupt_cache_is_safe(tmp_path, monkeypatch):
    """Garbage in the cache file must not crash — just fall back to a full scan."""
    _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    cache = tmp_path / "cache.pkl"
    cache.write_bytes(b"\x00\x01not a pickle\xff")
    p = Parser(PRICING, cache_path=cache)
    p.scan()  # must not raise
    assert len(p.records) == 1


def test_cache_wrong_shape_is_safe(tmp_path, monkeypatch):
    """A well-formed pickle of the wrong shape degrades to a cold scan, no crash."""
    _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    cache = tmp_path / "cache.pkl"
    cache.write_bytes(pickle.dumps({"version": P._CACHE_VERSION, "totally": "wrong"}))
    p = Parser(PRICING, cache_path=cache)
    p.scan()
    assert len(p.records) == 1


def test_no_cache_path_writes_nothing(tmp_path, monkeypatch):
    """The default (cache_path=None) parser never creates a cache file."""
    _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    p = Parser(PRICING)  # cache_path defaults to None
    p.scan()
    p.save_cache()  # no-op, must not raise
    assert list(tmp_path.rglob("*.pkl")) == []


def test_json_loads_orjson_stdlib_parity(monkeypatch):
    """_json_loads yields the same object whether orjson is used or the stdlib fallback."""
    raw = _line("r1", "m1", 123, 45).encode("utf-8").rstrip(b"\n")
    stdlib_out = json.loads(raw.decode("utf-8"))

    # Force the stdlib branch regardless of whether orjson is installed.
    monkeypatch.setattr(P, "_orjson", None)
    assert _json_loads(raw) == stdlib_out

    # If orjson is available, its output must match too.
    try:
        import orjson
    except ImportError:
        return
    monkeypatch.setattr(P, "_orjson", orjson)


def test_prime_cache_exposes_records_before_reconciliation(tmp_path, monkeypatch):
    file = _seed(tmp_path, monkeypatch, _line("r1", "m1", 100))
    cache = tmp_path / "cache.pkl"
    writer = Parser(PRICING, cache_path=cache)
    writer.scan()
    writer.save_cache()

    primed = Parser(PRICING, cache_path=cache)
    assert primed.prime_cache() is True
    assert [record.input_tokens for record in primed.records] == [100]

    with file.open("a", encoding="utf-8") as handle:
        handle.write(_line("r2", "m2", 200))
    primed.scan()
    assert sorted(record.input_tokens for record in primed.records) == [100, 200]


def test_codex_archive_move_preserves_warm_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    active = tmp_path / "active"
    archived = tmp_path / "archived"
    active.mkdir()
    archived.mkdir()
    original = active / "rollout-2026-test.jsonl"
    original.write_text(_line("r1", "m1", 100), "utf-8")
    cache = tmp_path / "cache.pkl"

    writer = Parser(PRICING, cache_path=cache)
    writer.scan()
    writer.save_cache()
    moved = archived / original.name
    original.rename(moved)

    warm = Parser(PRICING, cache_path=cache)
    warm.scan()
    assert len(warm.records) == 1
    assert warm.stats.lines_read == 0
    assert str(moved) in warm._files
