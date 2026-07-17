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


def test_stale_cache_version_is_ignored(tmp_path, monkeypatch):
    import cc_usage.parser as parser_module

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        _rate_line("2026-07-16T10:00:00Z", primary=_win(28.0, 10080, 222)), "utf-8"
    )
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", tmp_path)
    cache = tmp_path / "parse-cache.pkl"
    cold = Parser({}, cache_path=cache)
    cold.scan()
    cold.save_cache()

    # Simulate a format upgrade: the on-disk cache now predates the running version.
    monkeypatch.setattr(parser_module, "_CACHE_VERSION", parser_module._CACHE_VERSION + 1)
    warm = Parser({}, cache_path=cache)
    assert warm.prime_cache() is False  # stale version rejected, not primed
    warm.scan()  # rebuilds straight from disk
    capture = next(iter(warm.latest_rate_limits_by_account.values()))
    assert get_buckets(capture)[0].used_percentage == 28.0
