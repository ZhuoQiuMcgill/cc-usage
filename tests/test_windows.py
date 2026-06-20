"""Rolling-window aggregation (T0 §5): rows straddling each boundary land in
exactly the right windows."""

from cc_usage.aggregate import aggregate
from cc_usage.parser import UsageRecord

NOW = 1_000_000_000.0


def rec(age_s: float, inp: int, model: str = "claude-opus-4-8", cost: float = 0.0):
    return UsageRecord(
        ts=NOW - age_s,
        model_raw=model,
        model_norm=model,
        known=True,
        input_tokens=inp,
        output_tokens=0,
        cache_read=0,
        cache_creation=0,
        cost=cost,
    )


# Offsets chosen to straddle each boundary, including exact-edge cases.
# T3 R1 adds 7d (168h): a record at 3d lands in 7d + all (not 24h), and one exactly at
# 168h is included in 7d while 168h+1s is excluded.
RECORDS = [
    rec(1_800, 10),  # 30m  -> 1h,5h,24h,7d,all
    rec(3_600, 100),  # exactly 1h -> included in 1h (age <= window)
    rec(3_601, 7),  # 1s past 1h -> excluded from 1h
    rec(7_200, 1_000),  # 2h   -> 5h,24h,7d,all
    rec(36_000, 10_000),  # 10h  -> 24h,7d,all
    rec(259_200, 50_000),  # 3d   -> 7d,all (not 24h)
    rec(7 * 24 * 3600, 3),  # exactly 7d -> included in 7d (age <= window)
    rec(7 * 24 * 3600 + 1, 8),  # 1s past 7d -> excluded from 7d
    rec(1_209_600, 100_000),  # 14d  -> all only
]


def test_window_inclusion_boundaries():
    w = aggregate(RECORDS, NOW)
    assert w["1h"].input_tokens == 110  # 10 + 100
    assert w["5h"].input_tokens == 1_117  # + 7 + 1000
    assert w["24h"].input_tokens == 11_117  # + 10000
    # 7d adds the 3d row (50000) and the exact-168h row (3), but NOT 168h+1s (8):
    assert w["7d"].input_tokens == 11_117 + 50_000 + 3  # = 61_120
    assert w["all"].input_tokens == 61_120 + 8 + 100_000  # = 161_128


def test_7d_between_24h_and_all():
    """R1: the 7d window is monotonic between 24h and all-time."""
    w = aggregate(RECORDS, NOW)
    assert w["24h"].input_tokens <= w["7d"].input_tokens <= w["all"].input_tokens
    # 7d must actually capture the 3-day-old record that 24h misses.
    assert w["7d"].input_tokens > w["24h"].input_tokens


def test_7d_exact_boundary_inclusion():
    """age == 7d included (<=), age == 7d + 1s excluded — mirrors the other windows."""
    on_edge = [rec(7 * 24 * 3600, 5)]
    just_past = [rec(7 * 24 * 3600 + 1, 5)]
    assert aggregate(on_edge, NOW)["7d"].input_tokens == 5
    assert aggregate(just_past, NOW)["7d"].input_tokens == 0


def test_all_time_includes_oldest():
    w = aggregate(RECORDS, NOW)
    assert any(True for _ in w["all"].models)  # not empty
    assert w["all"].input_tokens >= w["24h"].input_tokens >= w["1h"].input_tokens


def test_per_model_split_within_window():
    records = [rec(60, 5, "claude-opus-4-8"), rec(60, 50, "claude-sonnet-4-6")]
    w = aggregate(records, NOW)
    m = w["1h"].models
    assert set(m) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert m["claude-sonnet-4-6"].input_tokens == 50
    assert m["claude-opus-4-8"].input_tokens == 5


def test_cost_sum_and_sort_order():
    records = [
        rec(10, 0, "claude-opus-4-8", cost=2.0),
        rec(10, 0, "claude-sonnet-4-6", cost=5.0),
    ]
    w = aggregate(records, NOW)
    assert abs(w["all"].cost - 7.0) < 1e-9
    order = [m.model for m in w["all"].models_sorted()]
    assert order[0] == "claude-sonnet-4-6"  # higher cost sorts first


def test_total_and_cache_tokens():
    r = UsageRecord(
        ts=NOW - 10,
        model_raw="m",
        model_norm="claude-opus-4-8",
        known=True,
        input_tokens=1,
        output_tokens=2,
        cache_read=3,
        cache_creation=4,
        cost=0.0,
    )
    w = aggregate([r], NOW)
    assert w["all"].cache_tokens == 7
    assert w["all"].total_tokens == 10
