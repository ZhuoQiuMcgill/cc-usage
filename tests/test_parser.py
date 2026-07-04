"""Parser dedup + tolerant extraction against a hand-verified fixture (T0 §10.2).

The fixture deliberately includes a malformed line, an unknown model id, a
streaming pair (two lines sharing one (requestId, message.id): a partial
message_start snapshot then the final counts), a non-assistant line, and an
assistant line with no usage object.
"""

import json
import math
from pathlib import Path

from cc_usage.cost import compute_cost
from cc_usage.parser import Parser

FIXTURE = Path(__file__).parent / "fixtures" / "sample.jsonl"
PRICING = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}


def _parse() -> Parser:
    p = Parser(PRICING)
    p.ingest_file(FIXTURE)
    return p


def test_counts_dedup_and_malformed():
    p = _parse()
    assert p.stats.records == 4  # A, B, C, D (A-dup skipped)
    assert p.stats.duplicates == 1  # the second req_A/msg_A line
    assert p.stats.malformed == 1  # the broken-JSON line


def test_unknown_model_kept_but_zero_cost_and_flagged():
    p = _parse()
    assert "claude-mystery-9" in p.stats.unknown_models
    c = next(r for r in p.records if r.model_norm == "claude-mystery-9")
    assert c.known is False
    assert c.cost == 0.0
    assert c.input_tokens == 1000 and c.output_tokens == 1000  # tokens still counted


def test_token_totals_match_hand_sum():
    p = _parse()
    assert sum(r.input_tokens for r in p.records) == 2700
    assert sum(r.output_tokens for r in p.records) == 3150
    assert sum(r.cache_read for r in p.records) == 12000
    assert sum(r.cache_creation for r in p.records) == 4800


def test_total_cost_matches_hand_sum():
    p = _parse()
    total = sum(r.cost for r in p.records)
    assert math.isclose(total, 0.1051, abs_tol=1e-9)


def test_1m_suffix_merges_into_opus_group():
    p = _parse()
    opus = [r for r in p.records if r.model_norm == "claude-opus-4-8"]
    assert len(opus) == 2  # record A and record D[1m]
    assert math.isclose(sum(r.cost for r in opus), 0.0985, abs_tol=1e-9)


def test_streaming_lines_merge_to_final_not_first():
    # msg_A streams as two lines sharing (req_A, msg_A): a partial message_start
    # snapshot (output 1) then the final line (output 2000), with identical
    # input/cache. The kept record must carry the FINAL output — first-wins dedup
    # (the T9 bug) would have kept the partial 1 — and must not SUM the identical
    # input/cache fields.
    p = _parse()
    a = next(r for r in p.records if r.cache_read == 10000)  # msg_A is the only cached row
    assert a.output_tokens == 2000  # final line's count, not the partial 1
    assert a.input_tokens == 1000  # max/identical, not summed to 2000
    assert a.cache_read == 10000  # not summed to 20000
    assert a.cache_creation == 4000  # not summed to 8000


# ── streaming-usage merge (T9) ─────────────────────────────────────────────
# Claude Code writes several transcript lines for one streaming assistant reply,
# all sharing (requestId, message.id); only output_tokens grows across them. Repeat
# lines are merged (field-wise max of the counters, cost recomputed), not dropped.

RATES_OPUS = (5.0, 25.0)  # == PRICING["claude-opus-4-8"] (input, output) per 1M


def _asst_line(
    req,
    mid,
    *,
    inp=0,
    out=0,
    cache_read=0,
    cache_creation=0,
    eph_5m=None,
    eph_1h=None,
    model="claude-opus-4-8",
    ts="2026-06-01T00:00:00.000Z",
):
    """One assistant-usage JSONL line with full control over the counters.

    A cache_creation sub-bucket object is emitted only when a sub-bucket is given,
    so the None-vs-0 path in compute_cost can be exercised. `mid=None` omits
    message.id entirely (an un-dedupable line)."""
    usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if eph_5m is not None or eph_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": eph_5m or 0,
            "ephemeral_1h_input_tokens": eph_1h or 0,
        }
    message = {"model": model, "usage": usage}
    if mid is not None:
        message["id"] = mid
    return (
        json.dumps(
            {"type": "assistant", "requestId": req, "timestamp": ts, "message": message}
        )
        + "\n"
    )


def _parse_lines(tmp_path, lines, pricing=PRICING):
    f = tmp_path / "s.jsonl"
    f.write_text("".join(lines))
    p = Parser(pricing)
    p.ingest_file(f)
    return p


def test_streaming_lines_merge_keep_final_output(tmp_path):
    lines = [
        _asst_line("r", "m", inp=1000, out=7, cache_read=500, cache_creation=200),
        _asst_line("r", "m", inp=1000, out=500, cache_read=500, cache_creation=200),
        _asst_line("r", "m", inp=1000, out=2000, cache_read=500, cache_creation=200),
    ]
    p = _parse_lines(tmp_path, lines)
    assert p.stats.records == 1
    assert p.stats.duplicates == 2  # two later lines merged into the first
    assert len(p.records) == 1
    rec = p.records[0]
    assert rec.output_tokens == 2000
    assert rec.input_tokens == 1000
    # cost recomputed from the FINAL counters (no sub-buckets -> 1.25x creation fallback):
    #   1000*5 + 2000*25 + 500*5*0.10 + 200*5*1.25, all / 1e6 = 0.0565
    assert math.isclose(rec.cost, 0.0565, abs_tol=1e-9)


def test_merge_takes_field_max_not_sum(tmp_path):
    # out-of-order outputs (500, 2000, 7): the max (2000, the middle line) must win —
    # not first, not last, not the sum — and identical input/cache must not accumulate.
    lines = [
        _asst_line("r", "m", inp=1000, out=500, cache_read=800, cache_creation=300),
        _asst_line("r", "m", inp=1000, out=2000, cache_read=800, cache_creation=300),
        _asst_line("r", "m", inp=1000, out=7, cache_read=800, cache_creation=300),
    ]
    p = _parse_lines(tmp_path, lines)
    assert len(p.records) == 1
    r = p.records[0]
    assert r.output_tokens == 2000  # max, not first(500)/last(7)/sum(2507)
    assert r.input_tokens == 1000  # not summed to 3000
    assert r.cache_read == 800  # not summed to 2400
    assert r.cache_creation == 300  # not summed to 900


def test_distinct_message_ids_stay_separate(tmp_path):
    lines = [
        _asst_line("r1", "m1", inp=100, out=10),
        _asst_line("r2", "m2", inp=200, out=20),
    ]
    p = _parse_lines(tmp_path, lines)
    assert len(p.records) == 2
    assert p.stats.duplicates == 0
    assert sum(r.output_tokens for r in p.records) == 30


def test_lines_without_message_id_are_not_merged(tmp_path):
    # no message.id -> _dedup_key is None -> each line counts as its own record.
    lines = [
        _asst_line("r1", None, inp=100, out=10),
        _asst_line("r1", None, inp=200, out=20),
    ]
    p = _parse_lines(tmp_path, lines)
    assert len(p.records) == 2
    assert p.stats.duplicates == 0


def test_merge_preserves_subbuckets(tmp_path):
    # lines carry cache_creation sub-buckets -> merged cost uses the 5m(1.25x)/1h(2x)
    # path, NOT the aggregate 1.25x fallback.
    lines = [
        _asst_line("r", "m", inp=1000, out=7, cache_creation=4000, eph_5m=1000, eph_1h=3000),
        _asst_line("r", "m", inp=1000, out=2000, cache_creation=4000, eph_5m=1000, eph_1h=3000),
    ]
    p = _parse_lines(tmp_path, lines)
    assert len(p.records) == 1
    r = p.records[0]
    expected = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=0,
        cache_creation_total=4000,
        ephemeral_5m=1000,
        ephemeral_1h=3000,
        rates=RATES_OPUS,
    )
    assert math.isclose(r.cost, expected, abs_tol=1e-12)
    fallback = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=0,
        cache_creation_total=4000,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=RATES_OPUS,
    )
    assert not math.isclose(r.cost, fallback, abs_tol=1e-9)  # sub-buckets, not fallback


def test_merge_without_subbuckets_uses_fallback(tmp_path):
    # no cache_creation object on any line -> merged cost uses the 1.25x aggregate fallback.
    lines = [
        _asst_line("r", "m", inp=1000, out=7, cache_creation=4000),
        _asst_line("r", "m", inp=1000, out=2000, cache_creation=4000),
    ]
    p = _parse_lines(tmp_path, lines)
    r = p.records[0]
    fallback = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=0,
        cache_creation_total=4000,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=RATES_OPUS,
    )
    assert math.isclose(r.cost, fallback, abs_tol=1e-12)
