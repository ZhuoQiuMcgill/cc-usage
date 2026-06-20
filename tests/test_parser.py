"""Parser dedup + tolerant extraction against a hand-verified fixture (T0 §10.2).

The fixture deliberately includes a malformed line, an unknown model id, a
duplicate (requestId, message.id), a non-assistant line, and an assistant line
with no usage object.
"""

import math
from pathlib import Path

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


def test_duplicate_inflated_values_did_not_leak():
    # the duplicate carried 999999s; if dedup failed totals would explode
    p = _parse()
    assert all(r.input_tokens < 999999 for r in p.records)
