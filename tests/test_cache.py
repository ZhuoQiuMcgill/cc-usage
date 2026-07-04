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
import pickle

import cc_usage.parser as P
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
        + _line("r3", "m3", 300, 30, model="claude-unknown-9")  # unknown model, cost 0
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
    assert _json_loads(raw) == stdlib_out
