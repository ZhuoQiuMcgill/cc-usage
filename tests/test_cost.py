"""Cost model + model-id normalization (T0 §6). Numbers verified by hand."""

import math

from cc_usage.cost import compute_cost, get_rates, normalize_model

PRICING = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}


def test_normalize_strips_suffixes():
    assert normalize_model("claude-opus-4-8[1m]") == "claude-opus-4-8"
    assert normalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert normalize_model("claude-sonnet-4-6-20251001") == "claude-sonnet-4-6"
    assert normalize_model("us.anthropic.claude-opus-4-8") == "claude-opus-4-8"
    assert normalize_model("  Claude-Opus-4-8  ") == "claude-opus-4-8"
    assert normalize_model(None) == ""
    assert normalize_model("") == ""


def test_get_rates_known_and_unknown():
    assert get_rates("claude-opus-4-8", PRICING) == (5.0, 25.0)
    assert get_rates("claude-opus-4-8[1m]", PRICING) == (5.0, 25.0)
    assert get_rates("claude-mystery-9", PRICING) is None
    assert get_rates(None, PRICING) is None


def test_bundled_pricing_prices_sonnet_5():
    """Claude Sonnet 5 (claude-sonnet-5) ships in the pricing table at $3/$15 and
    resolves through the tolerant matcher, including a [1m] variant."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    assert models["claude-sonnet-5"] == {"input": 3.0, "output": 15.0}
    assert get_rates("claude-sonnet-5", models) == (3.0, 15.0)
    assert get_rates("claude-sonnet-5[1m]", models) == (3.0, 15.0)


def test_cost_with_ephemeral_subbuckets():
    # Record A: in 1000, out 2000, cache_read 10000, eph_5m 1000, eph_1h 3000 (opus 5/25).
    # 0.005 + 0.05 + 0.005 + 0.00625 + 0.03 = 0.09625
    cost = compute_cost(
        input_tokens=1000,
        output_tokens=2000,
        cache_read=10000,
        cache_creation_total=4000,
        ephemeral_5m=1000,
        ephemeral_1h=3000,
        rates=(5.0, 25.0),
    )
    assert math.isclose(cost, 0.09625, abs_tol=1e-9)


def test_cost_fallback_to_aggregate_when_subbuckets_absent():
    # Record B: no cache_creation object -> aggregate 800 * 1.25 (sonnet 3/15).
    # 0.0015 + 0.0015 + 0.0006 + 0.003 = 0.0066
    cost = compute_cost(
        input_tokens=500,
        output_tokens=100,
        cache_read=2000,
        cache_creation_total=800,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=(3.0, 15.0),
    )
    assert math.isclose(cost, 0.0066, abs_tol=1e-9)


def test_unknown_model_costs_zero():
    cost = compute_cost(
        input_tokens=1000,
        output_tokens=1000,
        cache_read=5,
        cache_creation_total=5,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=None,
    )
    assert cost == 0.0


def test_subbuckets_differ_from_fallback():
    """1h tokens cost 2.0x but the aggregate fallback would only charge 1.25x —
    proves we use the sub-buckets when present rather than the aggregate."""
    via_buckets = compute_cost(
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_creation_total=1000,
        ephemeral_5m=0,
        ephemeral_1h=1000,
        rates=(5.0, 25.0),
    )
    via_fallback = compute_cost(
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_creation_total=1000,
        ephemeral_5m=None,
        ephemeral_1h=None,
        rates=(5.0, 25.0),
    )
    assert math.isclose(via_buckets, 1000 * 5e-6 * 2.00, abs_tol=1e-12)
    assert math.isclose(via_fallback, 1000 * 5e-6 * 1.25, abs_tol=1e-12)
    assert via_buckets > via_fallback
