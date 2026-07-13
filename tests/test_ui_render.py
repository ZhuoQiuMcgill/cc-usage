"""Responsive main-panel rendering and UX copy."""

import io

from rich.console import Console

from cc_usage.aggregate import ModelAgg, WindowAgg, aggregate_range
from cc_usage.config import Config
from cc_usage.parser import UsageRecord
from cc_usage.render import (
    RenderState,
    footnotes,
    model_block,
    range_model_block,
    range_totals_block,
    spend_block,
)
from cc_usage.themes import get_theme


def _plain(renderable, width=100):
    buffer = io.StringIO()
    Console(file=buffer, width=width, no_color=True).print(renderable)
    return buffer.getvalue()


def _state(compact):
    windows = {
        key: WindowAgg(
            name=key,
            input_tokens=1200,
            output_tokens=300,
            cache_tokens=8500,
            cost=4.75,
        )
        for key in ("1h", "5h", "24h", "7d", "all")
    }
    windows["all"].models["claude-opus-4-8"] = ModelAgg(
        model="claude-opus-4-8",
        input_tokens=1200,
        output_tokens=300,
        cache_tokens=8500,
        cost=4.75,
    )
    return RenderState(
        windows=windows,
        buckets=[],
        now=1_000_000_000,
        config=Config(default_window="all"),
        interval=5,
        compact=compact,
    )


def test_compact_spend_transposes_windows_without_losing_any():
    output = _plain(spend_block(_state(True), get_theme("dark")), width=60)
    assert "Rolling usage" in output
    assert "Window" in output and "Tokens" in output and "Cost" in output
    for label in ("1h", "5h", "24h", "7d", "all-time"):
        assert label in output


def test_compact_models_collapses_token_columns():
    output = _plain(model_block(_state(True), get_theme("dark")), width=60)
    assert "Models · all-time" in output
    assert "Tokens" in output
    assert " In " not in output and " Out " not in output and " Cache " not in output
    assert "Opus 4.8" in output and "Total" in output


def test_wide_models_preserves_token_breakdown():
    output = _plain(model_block(_state(False), get_theme("dark")))
    assert "In" in output and "Out" in output and "Cache" in output


def test_unpriced_usage_is_not_presented_as_free():
    state = _state(False)
    for window in state.windows.values():
        window.cost = 0.0
        window.unpriced_tokens = window.total_tokens
    state.windows["all"].models = {
        "codex-auto-review": ModelAgg(
            model="codex-auto-review",
            known=False,
            input_tokens=900,
            output_tokens=100,
            cost=0.0,
        )
    }
    state.unknown_models = {"codex-auto-review"}

    spend = _plain(spend_block(state, get_theme("dark")))
    models = _plain(model_block(state, get_theme("dark")))
    notes = "\n".join(_plain(note) for note in footnotes(state, get_theme("dark")))

    assert "unpriced" in spend
    assert "unpriced" in models
    assert "kept at $0" not in notes
    assert "price unavailable" in notes
    assert "0.0% of all-time tokens priced" in notes


def test_mixed_priced_and_unpriced_cost_uses_known_amount_without_suffix():
    state = _state(False)
    for window in state.windows.values():
        window.unpriced_tokens = 1000
    output = _plain(spend_block(state, get_theme("dark")))
    assert "$4.75" in output
    assert "+ ?" not in output


def test_range_aggregation_and_render_keep_unpriced_cost_explicit():
    record = UsageRecord(
        ts=1_000_000_000,
        model_raw="codex-auto-review",
        model_norm="codex-auto-review",
        known=False,
        input_tokens=900,
        output_tokens=100,
        cache_read=0,
        cache_creation=0,
        cost=0.0,
    )
    rng = aggregate_range([record], record.ts - 60, record.ts + 60)
    assert rng.unpriced_tokens == 1000
    assert rng.pricing_coverage == 0.0

    totals = _plain(range_totals_block(rng, get_theme("dark")))
    models = _plain(range_model_block(rng, get_theme("dark")))
    assert "unpriced" in totals and "0.0% of tokens" in totals
    assert "unpriced" in models
