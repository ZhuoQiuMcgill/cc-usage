"""Codex / ChatGPT rollout parsing and rate-limit normalization."""

import json
import math

from cc_usage.accounts import CODEX_ACCOUNT
from cc_usage.parser import Parser
from cc_usage.ratelimits import account_buckets, get_buckets


def _line(obj):
    return json.dumps(obj) + "\n"


def _context(model="gpt-test"):
    return _line(
        {
            "timestamp": "2026-07-12T12:00:00Z",
            "type": "turn_context",
            "payload": {"model": model},
        }
    )


def _tokens(ts, total, last, *, pct=25, minutes=10080, reset=2_000_000_000):
    return _line(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total[0],
                        "cached_input_tokens": total[1],
                        "output_tokens": total[2],
                        "total_tokens": total[0] + total[2],
                    },
                    "last_token_usage": {
                        "input_tokens": last[0],
                        "cached_input_tokens": last[1],
                        "output_tokens": last[2],
                        "total_tokens": last[0] + last[2],
                    },
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": pct,
                        "window_minutes": minutes,
                        "resets_at": reset,
                    },
                    "secondary": None,
                },
            },
        }
    )


def _win(pct, minutes, reset):
    return {"used_percent": pct, "window_minutes": minutes, "resets_at": reset}


def _rate_line(ts, primary=None, secondary=None):
    """A token_count event carrying only a rate-limit snapshot (no token info), so it
    exercises the snapshot capture in isolation without also producing a usage record."""
    return _line(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {"primary": primary, "secondary": secondary},
            },
        }
    )


def test_codex_uses_per_response_delta_and_splits_cached_input(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _context()
        + _tokens("2026-07-12T12:00:01Z", (100, 40, 10), (100, 40, 10))
        + _tokens("2026-07-12T12:00:02Z", (250, 100, 30), (150, 60, 20)),
        "utf-8",
    )
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}
    parser = Parser(pricing)
    parser.ingest_file(path)

    assert len(parser.records) == 2
    assert sum(r.input_tokens for r in parser.records) == 150
    assert sum(r.cache_read for r in parser.records) == 100
    assert sum(r.output_tokens for r in parser.records) == 30
    assert sum(r.total_tokens for r in parser.records) == 280
    assert all(r.model_norm == "gpt-test" for r in parser.records)
    expected = (150 * 2 + 100 * 2 * 0.1 + 30 * 8) / 1_000_000
    assert math.isclose(sum(r.cost for r in parser.records), expected)


def test_codex_backfills_tokens_before_first_turn_context(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _tokens("2026-07-12T12:00:01Z", (100, 40, 10), (100, 40, 10))
        + _context("gpt-test"),
        "utf-8",
    )
    parser = Parser({"gpt-test": {"input": 2.0, "output": 8.0}})
    parser.ingest_file(path)

    assert len(parser.records) == 1
    record = parser.records[0]
    assert record.model_norm == "gpt-test"
    assert record.known is True
    assert parser.stats.unknown_models == set()
    assert record.cost > 0.0


def test_codex_backfill_preserves_unpriced_authoritative_model(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _tokens("2026-07-12T12:00:01Z", (100, 0, 10), (100, 0, 10))
        + _context("codex-auto-review"),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)

    record = parser.records[0]
    assert record.model_norm == "codex-auto-review"
    assert record.known is False
    assert parser.stats.unknown_models == {"codex-auto-review"}


def test_codex_pending_attribution_survives_warm_cache(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _tokens("2026-07-12T12:00:01Z", (100, 0, 10), (100, 0, 10)),
        "utf-8",
    )
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}
    cache = tmp_path / "parse-cache.pkl"
    cold = Parser(pricing, cache_path=cache)
    cold._read_new(path)
    assert cold.records[0].model_norm == "codex-unattributed"
    cold.save_cache()

    with path.open("a", encoding="utf-8") as stream:
        stream.write(_context("gpt-test"))

    warm = Parser(pricing, cache_path=cache)
    assert warm.prime_cache()
    warm._read_new(path)
    assert warm.records[0].model_norm == "gpt-test"
    assert warm.records[0].known is True
    assert warm.stats.unknown_models == set()


def test_codex_rate_limits_are_available_without_statusline(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _context()
        + _tokens(
            "2026-07-12T12:00:01Z",
            (100, 0, 10),
            (100, 0, 10),
            pct=37,
            minutes=10080,
            reset=2_000_000_000,
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)

    # No discovered root -> the snapshot lands under the default codex account label.
    capture = parser.latest_rate_limits_by_account[CODEX_ACCOUNT]
    assert capture["source"] == "codex"
    buckets = get_buckets(capture)
    assert len(buckets) == 1
    assert buckets[0].label == "WEEKLY"
    assert buckets[0].used_percentage == 37
    assert buckets[0].resets_at == 2_000_000_000


def test_provider_limits_are_combined_not_selected():
    claude = {
        "rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": 1000}
        }
    }
    codex = {
        "rate_limits": {
            "codex_primary": {
                "used_percentage": 34,
                "resets_at": 2000,
                "window_minutes": 10080,
            }
        }
    }
    buckets = account_buckets(
        {"claude:personal": claude, "codex:codex": codex},
        ["personal"],
        ["codex"],
        multi_claude=False,
        multi_codex=False,
    )
    assert [bucket.label for bucket in buckets] == ["CLAUDE 5-HOUR", "CODEX WEEKLY"]

def test_codex_limits_survive_warm_cache(tmp_path, monkeypatch):
    import cc_usage.parser as parser_module

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        _context() + _tokens("2026-07-12T12:00:01Z", (100, 0, 10), (100, 0, 10)),
        "utf-8",
    )
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    cache = tmp_path / "parse-cache.pkl"

    cold = Parser({}, cache_path=cache)
    cold.scan()
    cold.save_cache()
    warm = Parser({}, cache_path=cache)
    warm.scan()

    assert len(warm.records) == 1
    assert warm.latest_rate_limits_by_account == cold.latest_rate_limits_by_account
    capture = next(iter(warm.latest_rate_limits_by_account.values()))
    assert get_buckets(capture)[0].label == "WEEKLY"


# ── T13: rate-limit snapshots captured from rollouts, per codex root ─────────────
def _account_buckets(parser, label=CODEX_ACCOUNT):
    return get_buckets(parser.latest_rate_limits_by_account[label])


def test_snapshot_primary_only(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(28.0, 10080, 1_784_825_747)),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    buckets = _account_buckets(parser)
    assert [(b.label, b.used_percentage, b.resets_at) for b in buckets] == [
        ("WEEKLY", 28.0, 1_784_825_747)
    ]


def test_snapshot_primary_and_secondary(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _rate_line(
            "2026-07-16T11:00:00Z",
            primary=_win(28.0, 10080, 1_784_825_747),
            secondary=_win(5.0, 300, 1_700_000_300),
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    # codex_primary sorts before codex_secondary: weekly window then 5h window.
    assert [b.label for b in _account_buckets(parser)] == ["WEEKLY", "5-HOUR"]


def test_snapshot_null_primary_and_secondary_skipped(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(_rate_line("2026-05-01T10:00:00Z", None, None), "utf-8")
    parser = Parser({})
    parser.ingest_file(path)
    assert parser.latest_rate_limits_by_account == {}  # May-era null snapshot: nothing captured


def test_snapshot_malformed_window_skipped_not_fatal(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _rate_line(
            "2026-07-16T10:00:00Z",
            primary={"window_minutes": 10080, "resets_at": 1_784_825_747},  # used_percent missing
            secondary=_win(5.0, 300, 1_700_000_300),
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    assert [b.label for b in _account_buckets(parser)] == ["5-HOUR"]  # only the well-formed window


def test_snapshot_newest_wins_within_file(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(10.0, 10080, 111))
        + _rate_line("2026-07-16T12:00:00Z", primary=_win(28.0, 10080, 222))
        + _rate_line("2026-07-16T11:00:00Z", primary=_win(19.0, 10080, 333)),  # older ts, later line
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    b = _account_buckets(parser)[0]
    assert (b.used_percentage, b.resets_at) == (28.0, 222)  # newest by event timestamp


def test_snapshot_newest_wins_across_files_and_incremental_scans(tmp_path, monkeypatch):
    import cc_usage.parser as parser_module

    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    # Earlier-sorted file carries the newer timestamp -> it wins by ts, not file order.
    (tmp_path / "a.jsonl").write_text(
        _rate_line("2026-07-16T12:00:00Z", primary=_win(28.0, 10080, 222)), "utf-8"
    )
    later = tmp_path / "b.jsonl"
    later.write_text(_rate_line("2026-07-16T09:00:00Z", primary=_win(10.0, 10080, 111)), "utf-8")

    parser = Parser({})
    parser.scan()
    capture = next(iter(parser.latest_rate_limits_by_account.values()))
    assert get_buckets(capture)[0].used_percentage == 28.0

    # A newer snapshot appended on a later incremental scan replaces the current one.
    with later.open("a", encoding="utf-8") as fh:
        fh.write(_rate_line("2026-07-16T15:00:00Z", primary=_win(42.0, 10080, 444)))
    parser.scan()
    capture = next(iter(parser.latest_rate_limits_by_account.values()))
    assert get_buckets(capture)[0].used_percentage == 42.0


def test_snapshot_captured_per_codex_root(tmp_path):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "r.jsonl").write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(28.0, 10080, 111)), "utf-8"
    )
    (root_b / "r.jsonl").write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(4.0, 300, 222)), "utf-8"
    )
    parser = Parser({}, roots=[(root_a, "codex"), (root_b, "codex-win")])
    parser.scan()
    a = _account_buckets(parser, "codex")[0]
    b = _account_buckets(parser, "codex-win")[0]
    assert (a.label, a.used_percentage) == ("WEEKLY", 28.0)
    assert (b.label, b.used_percentage) == ("5-HOUR", 4.0)  # each root keeps its own limits


def test_snapshot_window_labels_and_value_passthrough(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _rate_line(
            "2026-07-16T10:00:00Z",
            primary=_win(28.5, 10080, 1_784_825_747),  # weekly
            secondary=_win(50.0, 4320, 999),  # 3 days -> humanized, non-standard window
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    labels = {b.label: b for b in _account_buckets(parser)}
    assert set(labels) == {"WEEKLY", "3-DAY"}  # 10080->WEEKLY, 4320->3-DAY
    assert labels["WEEKLY"].used_percentage == 28.5  # float used_percent preserved
    assert labels["WEEKLY"].resets_at == 1_784_825_747  # resets_at passthrough


def test_snapshot_five_hour_window_label(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(_rate_line("2026-07-16T10:00:00Z", primary=_win(4.0, 300, 555)), "utf-8")
    parser = Parser({})
    parser.ingest_file(path)
    assert _account_buckets(parser)[0].label == "5-HOUR"  # 300 minutes


def test_snapshot_captured_when_timestamp_absent(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _line(
            {  # no "timestamp" key at all
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {"primary": _win(28.0, 10080, 1_784_825_747), "secondary": None},
                },
            }
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    # Guards the capture-before-timestamp-guard placement: limits survive a missing ts.
    assert _account_buckets(parser)[0].used_percentage == 28.0


def test_snapshot_captured_when_timestamp_unparseable(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _line(
            {
                "timestamp": "not-a-real-date",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {"primary": _win(28.0, 10080, 1_784_825_747), "secondary": None},
                },
            }
        ),
        "utf-8",
    )
    parser = Parser({})
    parser.ingest_file(path)
    assert _account_buckets(parser)[0].used_percentage == 28.0


def test_snapshot_survives_active_to_archive_move(tmp_path):
    """Spec R1: an active->archive rollout move must not lose the snapshot. It is keyed by
    account label (not path), and the cache's basename remap keeps per-file state, so the
    warm scan after the move still reports the same limits."""
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    name = "rollout-2026-07-16T10-00-00-019f6cfe-144f-7001-b878-487df6d4efc6.jsonl"
    (active / name).write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(28.0, 10080, 222)), "utf-8"
    )
    cache = tmp_path / "parse-cache.pkl"
    roots = [(active, "codex"), (archived, "codex")]

    cold = Parser({}, cache_path=cache, roots=roots)
    cold.scan()
    cold.save_cache()
    assert _account_buckets(cold, "codex")[0].used_percentage == 28.0

    (active / name).rename(archived / name)  # active -> archive, same basename
    warm = Parser({}, cache_path=cache, roots=roots)
    warm.scan()
    assert _account_buckets(warm, "codex")[0].used_percentage == 28.0  # snapshot retained


def test_v6_shaped_cache_is_discarded_and_rebuilt(tmp_path, monkeypatch):
    """A real pre-T13 (v6) parse cache carries the old single `latest_rate_limits` key. It
    must be discarded on load — not mis-read — and rebuilt once from disk."""
    import pickle

    import cc_usage.parser as parser_module

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(28.0, 10080, 222)), "utf-8"
    )
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    cache = tmp_path / "parse-cache.pkl"

    fp = Parser({}, cache_path=cache)  # to compute matching fingerprints for the fake cache
    stale = {
        "version": 6,
        "pricing_fp": fp._pricing_fingerprint(),
        "roots_fp": fp._roots_fingerprint(),
        "files": {},
        "records": [],
        "keys": [],
        "codex": {
            "file_models": {},
            "pending": {},
            "totals": {},
            # The old single-snapshot key with a bogus 99% that must never surface.
            "latest_rate_limits": {
                "captured_at": 1.0,
                "source": "codex",
                "rate_limits": {"codex_primary": {"used_percentage": 99.0, "resets_at": 1.0}},
            },
        },
    }
    with cache.open("wb") as fh:
        pickle.dump(stale, fh)

    warm = Parser({}, cache_path=cache)
    assert warm.prime_cache() is False  # v6 rejected on version mismatch
    assert warm.latest_rate_limits_by_account == {}  # the stale 99% was not adopted
    warm.scan()  # rebuild straight from the rollout
    capture = next(iter(warm.latest_rate_limits_by_account.values()))
    assert get_buckets(capture)[0].used_percentage == 28.0  # real value, not the stale 99%


# ── Counting follows the cumulative counters (T17) ───────────────────────────────
def _counted(parser):
    """(input incl. cached, cached, output) summed over the parser's Codex records."""
    return (
        sum(r.input_tokens + r.cache_read for r in parser.records),
        sum(r.cache_read for r in parser.records),
        sum(r.output_tokens for r in parser.records),
    )


def _expected(events):
    """What a rollout's cumulative counters say it used: per counter segment, the final
    total minus where the segment started. The first segment starts at the inherited
    total (first total minus its own `last`); a fall in the total starts a new segment
    at zero."""
    used = [0, 0, 0]
    base = [t - l for t, l in zip(events[0][1], events[0][2])]
    previous = events[0][1]
    for _ts, total, _last in events[1:]:
        if any(now < before for now, before in zip(total, previous)):
            used = [u + p - b for u, p, b in zip(used, previous, base)]
            base = [0, 0, 0]
        previous = total
    return tuple(u + p - b for u, p, b in zip(used, previous, base))


# A real-shaped rollout: a token_count before the first turn_context, a re-emission at
# the same timestamp, one at a later timestamp, a counter jump with an empty `last` (a
# compaction call), a reset after compaction (the new total equals its `last`), and a
# zero-usage event after the reset.
_EVENTS = [
    ("2026-07-12T12:00:01.000Z", (1000, 200, 50), (1000, 200, 50)),
    ("2026-07-12T12:01:00.000Z", (3000, 1500, 90), (2000, 1300, 40)),
    ("2026-07-12T12:01:00.000Z", (3000, 1500, 90), (2000, 1300, 40)),  # exact repeat
    ("2026-07-12T12:01:30.000Z", (3000, 1500, 90), (2000, 1300, 40)),  # later re-emit
    ("2026-07-12T12:01:30.000Z", (3000, 1500, 90), (0, 0, 0)),  # nothing new
    ("2026-07-12T12:02:00.000Z", (7000, 4000, 120), (0, 0, 0)),  # jump, empty last
    ("2026-07-12T12:03:00.000Z", (9500, 6000, 160), (2500, 2000, 40)),
    ("2026-07-12T12:10:00.000Z", (800, 100, 20), (800, 100, 20)),  # reset
    ("2026-07-12T12:10:00.000Z", (800, 100, 20), (800, 100, 20)),  # repeat after it
    ("2026-07-12T12:11:00.000Z", (1300, 400, 45), (500, 300, 25)),
]


def _rollout(events, *, context_after=1, inherited=None):
    """JSONL for `events`. With `inherited`, a forked/resumed rollout: it opens with the
    parent's total and an empty `last`, and every total carries that base until the
    counters restart."""
    lines = []
    if inherited is not None:
        lines.append(_tokens("2026-07-12T11:59:00.000Z", inherited, (0, 0, 0)))
    base = inherited or (0, 0, 0)
    previous = None
    for index, (ts, total, last) in enumerate(events):
        if index == context_after:
            lines.append(_context("gpt-test"))
        if previous is not None and any(now < before for now, before in zip(total, previous)):
            base = (0, 0, 0)  # a reset drops the inherited base with everything else
        previous = total
        lines.append(_tokens(ts, tuple(t + b for t, b in zip(total, base)), last))
    return "".join(lines)


def test_codex_counts_only_when_the_cumulative_counters_advance(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(_rollout(_EVENTS), "utf-8")
    parser = Parser({"gpt-test": {"input": 2.0, "output": 8.0}})
    parser.ingest_file(path)

    assert len(parser.records) == 6  # the 4 re-emitted / empty events are not usage
    assert _counted(parser) == _expected(_EVENTS)
    # The event before the first turn_context was counted and re-attributed.
    assert all(r.model_norm == "gpt-test" and r.known for r in parser.records)
    # The jump with an empty `last` is counted from the counters, and the reset
    # counts its whole new total.
    assert (4000 - 2500, 2500, 30) in [
        (r.input_tokens, r.cache_read, r.output_tokens) for r in parser.records
    ]
    assert (700, 100, 20) in [
        (r.input_tokens, r.cache_read, r.output_tokens) for r in parser.records
    ]
    assert len({r.lkey for r in parser.records}) == 6


def test_codex_forked_rollout_does_not_recount_the_inherited_total(tmp_path):
    inherited = (5_000_000, 4_800_000, 12_000)
    path = tmp_path / "rollout.jsonl"
    path.write_text(_rollout(_EVENTS, inherited=inherited), "utf-8")
    parser = Parser({"gpt-test": {"input": 2.0, "output": 8.0}})
    parser.ingest_file(path)
    # Identical to the unforked rollout: the parent's usage is its own rollout's.
    assert _counted(parser) == _expected(_EVENTS)

    # A resumed rollout whose first event is a real turn on top of an inherited total
    # counts that turn (its `last`), not the inherited total.
    resumed = tmp_path / "resumed.jsonl"
    resumed.write_text(
        _tokens("2026-07-12T13:00:00.000Z", (5_001_000, 4_800_200, 12_050), (1000, 200, 50)),
        "utf-8",
    )
    fresh = Parser({"gpt-test": {"input": 2.0, "output": 8.0}})
    fresh.ingest_file(resumed)
    assert _counted(fresh) == (1000, 200, 50)


def test_codex_counting_is_the_same_incrementally_and_warm(tmp_path, monkeypatch):
    import cc_usage.parser as parser_module

    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path / "none")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "rollout-2026-07-12T12-00-00-019e71bb-f375-7731-9644-9a9412399f58.jsonl"
    text = _rollout(_EVENTS)
    lines = text.splitlines(keepends=True)
    cache = tmp_path / "cache.pkl"
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}
    roots = [(sessions, "codex")]

    # Written a few lines at a time — splitting re-emissions from their originals
    # across scans and across process restarts (warm cache).
    written = 0
    for chunk in (2, 3, 4, len(lines)):
        path.write_text("".join(lines[:chunk]), "utf-8")
        written = chunk
        parser = Parser(pricing, cache_path=cache, roots=roots)
        parser.scan()
        parser.save_cache()
    assert written == len(lines)
    once = Parser(pricing, roots=roots)
    once.scan()
    assert _counted(parser) == _counted(once) == _expected(_EVENTS)
    assert sorted(r.lkey for r in parser.records) == sorted(r.lkey for r in once.records)
