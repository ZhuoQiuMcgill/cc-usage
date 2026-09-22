"""Cost model + model-id normalization (T0 §6). Numbers verified by hand."""

import math

from cc_usage.cost import Rates, compute_cost, get_rates, normalize_model
from cc_usage.pricing import _coerce

PRICING = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}


def test_normalize_strips_suffixes():
    assert normalize_model("claude-opus-4-8[1m]") == "claude-opus-4-8"
    assert normalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert normalize_model("claude-sonnet-4-6-20251001") == "claude-sonnet-4-6"
    assert normalize_model("gpt-5.4-2026-03-05") == "gpt-5.4"
    assert normalize_model("us.anthropic.claude-opus-4-8") == "claude-opus-4-8"
    assert normalize_model("  Claude-Opus-4-8  ") == "claude-opus-4-8"
    assert normalize_model(None) == ""
    assert normalize_model("") == ""


def test_get_rates_known_and_unknown():
    assert get_rates("claude-opus-4-8", PRICING) == Rates(5.0, 25.0)
    assert get_rates("claude-opus-4-8[1m]", PRICING) == Rates(5.0, 25.0)
    assert get_rates("claude-mystery-9", PRICING) is None
    assert get_rates(None, PRICING) is None


def test_bundled_pricing_prices_sonnet_5():
    """Claude Sonnet 5 (claude-sonnet-5) ships at $2/$10 and resolves through the
    tolerant matcher, including a [1m] variant.

    The $2/$10 introductory rate replaced the original $3/$15 list price rather
    than expiring, so the table tracks $2/$10 as the standing rate."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    assert models["claude-sonnet-5"] == {"input": 2.0, "output": 10.0}
    assert get_rates("claude-sonnet-5", models) == Rates(2.0, 10.0)
    assert get_rates("claude-sonnet-5[1m]", models) == Rates(2.0, 10.0)


def test_bundled_pricing_prices_fable_5_1():
    """Claude Fable 5.1 (claude-fable-5-1) ships at $10/$50 — the same tier as
    Fable 5, which stays served under its own id.

    The point-release suffix must survive normalization: only a `-YYYYMMDD` date
    stamp is stripped, so `-5-1` stays put and resolves to its own row rather
    than collapsing into `claude-fable-5`."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    # Cache reads are $0.25 (0.025x input), not the 0.1x the engine derives by
    # default, so the rate must be stated explicitly.
    fable_5_1 = Rates(10.0, 50.0, cache_read=0.25)
    assert models["claude-fable-5-1"] == {"input": 10.0, "output": 50.0, "cache_read": 0.25}
    assert get_rates("claude-fable-5-1", models) == fable_5_1
    assert get_rates("claude-fable-5-1[1m]", models) == fable_5_1
    assert normalize_model("claude-fable-5-1") == "claude-fable-5-1"
    # Fable 5 keeps its own entry and is not shadowed by the point release.
    assert get_rates("claude-fable-5", models) == Rates(10.0, 50.0)


def test_bundled_pricing_prices_opus_5_5():
    """Claude Opus 5.5 (claude-opus-5-5) ships at $4/$20 with cache reads at $0.20
    (0.05x input, not the default 0.1x). Cache writes keep the standard 1.25x (5m)
    and 2x (1h) multipliers, so a mixed request prices to the published card."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    opus_5_5 = Rates(4.0, 20.0, cache_read=0.20)
    assert models["claude-opus-5-5"] == {"input": 4.0, "output": 20.0, "cache_read": 0.2}
    assert get_rates("claude-opus-5-5", models) == opus_5_5
    assert get_rates("claude-opus-5-5[1m]", models) == opus_5_5
    # Distinct row, not collapsed into claude-opus-5.
    assert get_rates("claude-opus-5", models) == Rates(5.0, 25.0)

    cost = compute_cost(
        input_tokens=100_000,
        output_tokens=10_000,
        cache_read=1_000_000,
        cache_creation_total=200_000,
        ephemeral_5m=100_000,
        ephemeral_1h=100_000,
        rates=opus_5_5,
    )
    # $4 in, $20 out, $0.20 cache read, $5 5m write, $8 1h write.
    expected = (100_000 * 4.0 + 10_000 * 20.0 + 1_000_000 * 0.20
                + 100_000 * 5.0 + 100_000 * 8.0) / 1_000_000
    assert math.isclose(cost, expected, abs_tol=1e-12)


def test_bundled_pricing_prices_opus_5():
    """Claude Opus 5 (claude-opus-5) ships in the pricing table at $5/$25 and
    resolves through the tolerant matcher, including the [1m] variant Claude Code
    writes for the 1M-context configuration."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    assert models["claude-opus-5"] == {"input": 5.0, "output": 25.0}
    assert get_rates("claude-opus-5", models) == Rates(5.0, 25.0)
    assert get_rates("claude-opus-5[1m]", models) == Rates(5.0, 25.0)
    # Must not collide with the 4-x aliases that share the same tier.
    assert get_rates("claude-opus-4-8", models) == Rates(5.0, 25.0)


def test_bundled_openai_pricing_uses_official_standard_rates():
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    assert models["gpt-5.6-sol"]["input"] == 5.0
    assert models["gpt-5.6-sol"]["cache_read"] == 0.5
    assert models["gpt-5.6-sol"]["cache_write"] == 6.25
    assert models["gpt-5.6-sol"]["output"] == 30.0
    assert models["gpt-5.6-terra"]["input"] == 2.5
    assert models["gpt-5.5"]["output"] == 30.0
    assert models["gpt-5.4"]["input"] == 2.5
    assert models["gpt-5.4-mini"] == {
        "input": 0.75,
        "cache_read": 0.075,
        "output": 4.5,
    }
    assert get_rates("gpt-5.4-2026-03-05", models) == get_rates("gpt-5.4", models)
    assert get_rates("gpt-5.6", models) == get_rates("gpt-5.6-sol", models)


def test_bundled_pricing_prices_gpt_6_astra():
    """GPT-6 Astra (gpt-6-astra) ships with the full OpenAI rate card: $10/$50
    standard, $1 cached input, $12.50 cache writes (1.25x input), and the 272K
    long-context tier at 2x input/cache and 1.5x output.

    Rates from https://developers.openai.com/api/docs/pricing (2026-09-01)."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    assert models["gpt-6-astra"] == {
        "input": 10.0,
        "cache_read": 1.0,
        "cache_write": 12.5,
        "output": 50.0,
        "long_context_threshold": 272000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    }
    rates = get_rates("gpt-6-astra", models)
    assert rates == Rates(
        input=10.0,
        output=50.0,
        cache_read=1.0,
        cache_write=12.5,
        long_context_threshold=272000,
        long_context_input_multiplier=2.0,
        long_context_output_multiplier=1.5,
    )
    # Cache writes are 1.25x the uncached input rate, per the published card.
    assert rates.cache_write == rates.input * 1.25
    # Distinct from the gpt-5.6 flagship it sits above, and not aliased to it.
    assert get_rates("gpt-6-astra", models) != get_rates("gpt-5.6-sol", models)


def test_gpt_6_astra_long_context_threshold_is_exclusive():
    """"More than 272K input tokens" - a prompt exactly at the threshold bills at
    the standard rate; one token over scales the whole request."""
    import json
    from importlib.resources import files

    models = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]
    rates = get_rates("gpt-6-astra", models)

    def cost(input_tokens: int, cache_read: int = 0) -> float:
        return compute_cost(
            input_tokens=input_tokens,
            output_tokens=0,
            cache_read=cache_read,
            cache_creation_total=0,
            ephemeral_5m=0,
            ephemeral_1h=0,
            rates=rates,
        )

    assert math.isclose(cost(272_000), 272_000 * 10.0 / 1e6, abs_tol=1e-12)
    assert math.isclose(cost(272_001), 272_001 * 20.0 / 1e6, abs_tol=1e-12)
    # The threshold counts cached tokens as input too, so a mostly-cached prompt
    # can cross it: 100K fresh + 200K cached = 300K -> long-context rates.
    assert math.isclose(
        cost(100_000, cache_read=200_000),
        (100_000 * 20.0 + 200_000 * 2.0) / 1e6,
        abs_tol=1e-12,
    )


def test_openai_long_context_and_explicit_cache_rates():
    rates = Rates(
        input=5.0,
        cache_read=0.5,
        cache_write=6.25,
        output=30.0,
        long_context_threshold=272_000,
        long_context_input_multiplier=2.0,
        long_context_output_multiplier=1.5,
    )
    cost = compute_cost(
        input_tokens=100_000,
        output_tokens=1_000,
        cache_read=200_000,
        cache_creation_total=0,
        ephemeral_5m=0,
        ephemeral_1h=0,
        rates=rates,
    )
    expected = (100_000 * 10.0 + 200_000 * 1.0 + 1_000 * 45.0) / 1_000_000
    assert math.isclose(cost, expected, abs_tol=1e-12)


def test_editable_pricing_preserves_optional_official_rate_fields():
    rows = _coerce(
        {
            "gpt-custom": {
                "input": "2.5",
                "cache_read": "0.25",
                "cache_write": "3.125",
                "output": "15",
                "long_context_threshold": "272000",
                "long_context_input_multiplier": "2",
                "long_context_output_multiplier": "1.5",
            }
        }
    )
    assert rows["gpt-custom"] == {
        "input": 2.5,
        "cache_read": 0.25,
        "cache_write": 3.125,
        "output": 15.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    }


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
