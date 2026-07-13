"""Codex / ChatGPT rollout parsing and rate-limit normalization."""

import json
import math

from cc_usage.parser import Parser
from cc_usage.ratelimits import get_buckets, provider_buckets


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

    capture = parser.latest_rate_limits
    assert capture is not None
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
    buckets = provider_buckets(claude, codex)
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
    assert warm.latest_rate_limits == cold.latest_rate_limits
    assert get_buckets(warm.latest_rate_limits)[0].label == "WEEKLY"
