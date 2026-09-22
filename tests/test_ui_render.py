"""Responsive main-panel rendering and UX copy."""

import io
import json
from importlib.resources import files

from rich.console import Console

import cc_usage.pricing as pricing_module
from cc_usage.aggregate import ModelAgg, WindowAgg, aggregate_range
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.format import human_rate
from cc_usage.parser import UsageRecord
from cc_usage.render import (
    RATE_FOOTNOTE,
    RenderState,
    build_panel,
    footnotes,
    model_block,
    range_model_block,
    range_totals_block,
    spend_block,
)
from cc_usage.themes import get_theme

BUNDLED = json.loads((files("cc_usage") / "data" / "pricing.json").read_text())["models"]


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


# ── T16: $/M rate columns on the Models board ─────────────────────────────────────


def _rated_state(models, *, pricing=BUNDLED, compact=False, show_cost=True):
    """A 7d-default state whose window holds `models` (ModelAgg list), priced by `pricing`."""
    win = WindowAgg(name="7d")
    for m in models:
        win.models[m.model] = m
        win.input_tokens += m.input_tokens
        win.output_tokens += m.output_tokens
        win.cache_tokens += m.cache_tokens
        win.cost += m.cost
        if not m.known:
            win.unpriced_tokens += m.total_tokens
    windows = {key: win for key in ("1h", "5h", "24h", "7d", "all")}
    return RenderState(
        windows=windows,
        buckets=[],
        now=1_000_000_000,
        config=Config(default_window="7d", show_cost=show_cost),
        interval=5,
        compact=compact,
        pricing=pricing,
    )


def _row(output, name):
    """Cells of the single rendered line that starts with `name` (panel borders dropped)."""
    lines = [line.replace("│", " ").strip() for line in output.splitlines()]
    hits = [line for line in lines if line.startswith(name)]
    assert len(hits) == 1, (name, output)
    return hits[0].split()


def _header(output):
    return next(
        line.replace("│", " ").split()
        for line in output.splitlines()
        if line.replace("│", " ").split()[:1] == ["Model"]
    )


def test_human_rate_uses_two_decimals_unless_that_loses_precision():
    assert human_rate(5.0) == "5.00"
    assert human_rate(25) == "25.00"
    assert human_rate(0.25) == "0.25"
    assert human_rate(0.1) == "0.10"
    assert human_rate(0.075) == "0.075"  # never 0.07 / 0.08
    assert human_rate(3.125) == "3.125"


def test_models_board_shows_rate_after_each_token_column():
    state = _rated_state(
        [
            ModelAgg(model="claude-opus-4-8", input_tokens=1200, output_tokens=300,
                     cache_tokens=8500, cost=4.75),
            ModelAgg(model="claude-opus-5-5", input_tokens=900, output_tokens=200,
                     cache_tokens=5000, cost=2.10),
            ModelAgg(model="gpt-5.4-mini", input_tokens=500, output_tokens=100,
                     cache_tokens=2000, cost=0.01),
        ]
    )
    out = _plain(model_block(state, get_theme("dark")))
    assert _header(out) == ["Model", "In", "$/M", "Out", "$/M", "Cache", "$/M", "Cost"]
    # Claude row without cache_read: cache rate derived as input x 0.1.
    assert _row(out, "Opus 4.8") == ["Opus", "4.8", "1K", "5.00", "300", "25.00", "8K", "0.50", "$4.75"]
    # Explicit cache_read wins over the derivation.
    assert _row(out, "Opus 5.5") == ["Opus", "5.5", "900", "4.00", "200", "20.00", "5K", "0.20", "$2.10"]
    # Three decimals where two would round the published rate off.
    assert _row(out, "gpt-5.4-mini") == ["gpt-5.4-mini", "500", "0.75", "100", "4.50", "2K", "0.075", "$0.01"]
    assert RATE_FOOTNOTE in out
    assert "published rate" in RATE_FOOTNOTE and "read rate" in RATE_FOOTNOTE


def test_unknown_model_rates_are_dashes_and_total_rates_are_blank():
    state = _rated_state(
        [
            ModelAgg(model="claude-opus-4-8", input_tokens=1200, output_tokens=300,
                     cache_tokens=8500, cost=4.75),
            ModelAgg(model="codex-auto-review", known=False, input_tokens=900, output_tokens=100),
        ]
    )
    out = _plain(model_block(state, get_theme("dark")))
    assert _row(out, "codex-auto-review") == [
        "codex-auto-review", "*", "900", "—", "100", "—", "0", "—", "unpriced"
    ]
    # Rates don't aggregate: the Total row carries tokens and cost only.
    assert _row(out, "Total") == ["Total", "2K", "400", "8K", "$4.75"]


def test_rate_columns_absent_in_compact_mode_and_without_cost():
    models = [ModelAgg(model="claude-opus-4-8", input_tokens=1200, output_tokens=300,
                       cache_tokens=8500, cost=4.75)]
    compact = _plain(model_block(_rated_state(models, compact=True), get_theme("dark")), width=60)
    assert "$/M" not in compact and RATE_FOOTNOTE not in compact
    assert "Tokens" in compact

    no_cost = _plain(model_block(_rated_state(models, show_cost=False), get_theme("dark")))
    assert "$/M" not in no_cost and RATE_FOOTNOTE not in no_cost
    assert _header(no_cost) == ["Model", "In", "Out", "Cache"]


def test_empty_window_keeps_the_plain_layout():
    out = _plain(model_block(_rated_state([]), get_theme("dark")), width=76)
    assert "no usage in last 7d" in out
    assert "$/M" not in out and RATE_FOOTNOTE not in out


def test_user_pricing_override_drives_the_rates_shown(tmp_path, monkeypatch):
    """R2: the board shows the engine's resolved card (user pricing.json over bundled),
    handed through the snapshot — not a second lookup of the bundled table."""
    user_file = tmp_path / "pricing.json"
    user_file.write_text(
        json.dumps({"models": {"claude-opus-4-8": {"input": 7.0, "output": 35.0}}}), "utf-8"
    )
    monkeypatch.setattr(pricing_module, "PRICING_JSON", user_file)
    monkeypatch.setattr(pricing_module, "ensure_dirs", lambda: None)

    eng = Engine(Config(default_window="all"), cache_path=None)
    eng.parser.records = [
        UsageRecord(
            ts=1_000_000_000, model_raw="claude-opus-4-8", model_norm="claude-opus-4-8",
            known=True, input_tokens=1200, output_tokens=300, cache_read=8500,
            cache_creation=0, cost=1.0,
        )
    ]
    eng._scanned = True
    state = eng.snapshot(1_000_000_000)
    assert state.pricing is eng.parser.pricing

    out = _plain(model_block(state, get_theme("dark")))
    # Bundled would read 5.00 / 25.00 / 0.50.
    assert _row(out, "Opus 4.8")[3:8] == ["7.00", "300", "35.00", "8K", "0.70"]


def test_full_models_board_fits_width_76_with_longest_real_names():
    """R8: at the narrowest full-layout width every row stays on one line, untruncated,
    even with the longest real ids and near-worst-case token/cost widths."""
    big = dict(input_tokens=999_900_000, output_tokens=999_900_000,
               cache_tokens=999_900_000, cost=12_345.67)
    state = _rated_state(
        [ModelAgg(model=mid, **big) for mid in ("gpt-5.6-terra", "gpt-6-astra", "claude-fable-5-1")]
    )
    out = _plain(build_panel(state), width=76)
    assert "…" not in out
    assert all(len(line) <= 76 for line in out.splitlines())
    rows = {
        "gpt-5.6-terra": ["2.50", "15.00", "0.25"],
        "gpt-6-astra": ["10.00", "50.00", "1.00"],
        "Fable 5.1": ["10.00", "50.00", "0.25"],
    }
    for name, (rin, rout, rcache) in rows.items():
        cells = _row(out, name)
        assert " ".join(cells[: len(name.split())]) == name  # full name, one line
        assert cells[len(name.split()) :] == [
            "999.9M", rin, "999.9M", rout, "999.9M", rcache, "$12,345.67"
        ]
    assert _header(out) == ["Model", "In", "$/M", "Out", "$/M", "Cache", "$/M", "Cost"]
    assert sum(RATE_FOOTNOTE in line for line in out.splitlines()) == 1


def test_long_unpriced_name_yields_before_any_number_at_width_76():
    """A long unpriced id (real data: `codex-unattributed`) can push the rated table past
    70 columns; the model name ellipsizes, token and cost cells never truncate."""
    state = _rated_state(
        [
            ModelAgg(model="gpt-5.6-sol", input_tokens=91_400_000, output_tokens=8_000_000,
                     cache_tokens=3_900_000_000, cost=12_639.20),
            ModelAgg(model="codex-unattributed", known=False, input_tokens=75_500_000,
                     output_tokens=5_600_000, cache_tokens=2_100_000_000),
        ]
    )
    out = _plain(build_panel(state), width=76)
    assert "unpriced" in out and "$12,639.20" in out
    assert _row(out, "gpt-5.6-sol")[1:] == ["91.4M", "5.00", "8.0M", "30.00", "3.9B", "0.50", "$12,639.20"]
    unpriced = _row(out, "codex-unattrib")
    assert unpriced[0].endswith("…")
    assert unpriced[1:] == ["75.5M", "—", "5.6M", "—", "2.1B", "—", "unpriced"]
    assert _row(out, "Total")[1:] == ["166.9M", "13.6M", "6.0B", "$12,639.20"]
