"""Responsive main-panel rendering and UX copy."""

import io
import json
from importlib.resources import files
from pathlib import Path

from rich.cells import cell_len
from rich.console import Console
from rich.measure import Measurement

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
import cc_usage.cost as cost_module
import cc_usage.engine as engine_module
from cc_usage.cli import run_once
from cc_usage.cost import Rates, compute_cost, get_rates
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

# A realistic 7d window: two Opus rows that share a prefix, the three gpt-5.6 tiers, and
# the long unpriced id real Codex data carries. `tests/fixtures/models_board_pre_t16.txt`
# holds main's (pre-T16) render of the layouts that must not change, captured from this
# exact builder.
_BOARD = [
    ("claude-opus-4-8", True, 3_400_000, 22_500_000, 1_800_000_000, 2158.96),
    ("claude-opus-4-7", True, 1_200_000, 9_100_000, 700_000_000, 901.10),
    ("gpt-5.6-sol", True, 91_400_000, 8_000_000, 3_900_000_000, 2639.20),
    ("gpt-5.6-terra", True, 4_000_000, 600_000, 200_000_000, 75.00),
    ("gpt-5.6-luna", True, 900_000, 100_000, 40_000_000, 5.25),
    ("codex-unattributed", False, 75_500_000, 5_600_000, 2_100_000_000, 0.0),
]


def _board_state(models=_BOARD, *, compact=False, show_cost=True, pricing=BUNDLED):
    win = WindowAgg(name="7d")
    for mid, known, inp, out, cache, cost in models:
        win.models[mid] = ModelAgg(
            model=mid, known=known, input_tokens=inp, output_tokens=out,
            cache_tokens=cache, cost=cost,
        )
        win.input_tokens += inp
        win.output_tokens += out
        win.cache_tokens += cache
        win.cost += cost
        if not known:
            win.unpriced_tokens += inp + out + cache
    return RenderState(
        windows={key: win for key in ("1h", "5h", "24h", "7d", "all")},
        buckets=[],
        now=1e9,
        config=Config(default_window="7d", show_cost=show_cost),
        interval=5,
        compact=compact,
        pricing=pricing,
    )


def _rated_state(models, *, pricing=BUNDLED, compact=False, show_cost=True):
    """A 7d-default state whose window holds `models` (ModelAgg list), priced by `pricing`."""
    return _board_state(
        [(m.model, m.known, m.input_tokens, m.output_tokens, m.cache_tokens, m.cost)
         for m in models],
        compact=compact, show_cost=show_cost, pricing=pricing,
    )


def _pre_t16(case):
    """One case of main's pre-T16 Models board render, byte for byte."""
    text = (Path(__file__).parent / "fixtures" / "models_board_pre_t16.txt").read_text("utf-8")
    sections = {}
    for chunk in text.split("### ")[1:]:
        name, _, body = chunk.partition("\n")
        sections[name] = body
    return sections[case]


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


_RATED_HEADER = ["Model", "In", "$/M", "Out", "$/M", "Cache", "$/M", "Cost"]


def _models_section(panel):
    """The Models board's lines out of a rendered panel (title through its last row)."""
    lines = [line.replace("│", " ").rstrip() for line in panel.splitlines()]
    start = next(i for i, line in enumerate(lines) if "Models ·" in line)
    end = next(
        (i for i in range(start + 1, len(lines)) if "───" in lines[i] or not lines[i].strip()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_human_rate_keeps_two_decimals_and_adds_only_what_the_rate_needs():
    assert human_rate(5.0) == "5.00"
    assert human_rate(25) == "25.00"
    assert human_rate(0.25) == "0.25"
    assert human_rate(0.1) == "0.10"
    assert human_rate(3 * 0.1) == "0.30"  # float noise is not "precision"
    assert human_rate(0.075) == "0.075"  # never 0.07 / 0.08
    assert human_rate(0.0375) == "0.0375"
    assert human_rate(0.0004) == "0.0004"
    assert human_rate(3.125) == "3.125"
    assert human_rate(1 / 3) == "0.333333"  # capped at six decimals


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
    assert _header(out) == _RATED_HEADER
    # Claude row without cache_read: cache rate derived as input x 0.1.
    assert _row(out, "Opus 4.8") == ["Opus", "4.8", "1K", "5.00", "300", "25.00", "8K", "0.50", "$4.75"]
    # Explicit cache_read wins over the derivation.
    assert _row(out, "Opus 5.5") == ["Opus", "5.5", "900", "4.00", "200", "20.00", "5K", "0.20", "$2.10"]
    # More decimals where two would round the rate off.
    assert _row(out, "gpt-5.4-mini") == ["gpt-5.4-mini", "500", "0.75", "100", "4.50", "2K", "0.075", "$0.01"]
    assert RATE_FOOTNOTE in out
    # Base (not "published": a user override isn't), Cache = read, dearer writes/long ctx.
    assert "base rate" in RATE_FOOTNOTE and "read rate" in RATE_FOOTNOTE
    assert "writes" in RATE_FOOTNOTE and "long context" in RATE_FOOTNOTE
    assert "published" not in RATE_FOOTNOTE


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


def test_compact_and_no_cost_layouts_are_byte_identical_to_main():
    theme = get_theme("dark")
    assert _plain(model_block(_board_state(compact=True), theme), width=60) == _pre_t16("compact_60")
    assert _plain(model_block(_board_state(show_cost=False), theme), width=100) == _pre_t16(
        "nocost_100"
    )


def test_empty_and_all_unpriced_windows_keep_mains_board():
    """Nothing to price -> no rate columns, no footnote: exactly the pre-T16 board."""
    theme = get_theme("dark")
    assert _plain(model_block(_board_state([]), theme)) == _pre_t16("empty_100")
    unpriced = _board_state([row for row in _BOARD if not row[1]])
    assert _plain(model_block(unpriced, theme)) == _pre_t16("unpriced_100")


def test_state_without_pricing_shows_no_rates():
    """A RenderState built without the engine's pricing must not dash out priced rows."""
    out = _plain(model_block(_board_state(pricing={}), get_theme("dark")))
    assert "$/M" not in out and RATE_FOOTNOTE not in out
    assert _header(out) == ["Model", "In", "Out", "Cache", "Cost"]


def test_narrow_full_layout_falls_back_to_mains_board():
    """`--once` never sets `compact`: below the width the rated board needs, the full
    layout renders exactly as before — no `$/M`, every name whole."""
    theme = get_theme("dark")
    # 44 / 54 / 64 are the content widths of a 50 / 60 / 70-column panel. (At 44 main
    # itself squeezes the numbers; the fallback reproduces it rather than doing worse.)
    assert _plain(model_block(_board_state(), theme), width=44) == _pre_t16("full_44")
    assert _plain(model_block(_board_state(), theme), width=54) == _pre_t16("full_54")
    assert _plain(model_block(_board_state(), theme), width=64) == _pre_t16("full_64")
    for width in (60, 70):
        out = _models_section(_plain(build_panel(_board_state()), width=width))
        assert "$/M" not in out and RATE_FOOTNOTE not in out and "…" not in out
        for name in ("Opus 4.8", "Opus 4.7", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                     "codex-unattributed *", "Total"):
            assert name in out, (width, name)


def test_every_width_fits_and_never_cuts_a_number():
    """Sweep the panel across widths: nothing overflows; when the rates show, every token,
    rate and cost cell is whole and the unpriced marker survives; when they don't, the
    Models board is main's."""
    numbers = ["91.4M", "8.0M", "3.9B", "$2,639.20", "176.4M", "45.9M", "8.7B",
               "$5,779.51", "30.00", "0.25", "6.00", "unpriced"]
    shown_from = None
    for width in range(40, 131):
        out = _plain(build_panel(_board_state()), width=width)
        assert all(len(line) <= width for line in out.splitlines()), width
        if "$/M" in out:
            shown_from = shown_from or width
            assert all(n in out for n in numbers), width
            assert all(n in out for n in ("gpt-5.6-terra", "gpt-5.6-luna", "Opus 4.7")), width
            assert _row(out, "codex-")[1] == "*", width
            assert sum(RATE_FOOTNOTE in line for line in out.splitlines()) == 1, width
        else:
            assert shown_from is None, f"rates vanished again at {width}"
    assert shown_from is not None and shown_from <= 76


def test_rates_drop_rather_than_squeeze_the_model_column_below_its_floor():
    """The Model column gives way to the rates only down to a 13-cell floor (the longest
    bundled priced name). Wide rates and costs that would need more fall back to the
    plain board — even though the footnote alone would fit."""
    pricing = {"acme-experimental-model": {"input": 12.3456, "output": 123.4567, "cache_read": 1.23456}}
    state = _board_state(
        [("acme-experimental-model", True, 999_900_000, 999_900_000, 999_900_000, 123_456.78)],
        pricing=pricing,
    )
    at_76 = _models_section(_plain(build_panel(state), width=76))
    assert _header(at_76) == ["Model", "In", "Out", "Cache", "Cost"]
    assert "acme-experimental-model" in at_76 and "…" not in at_76

    at_84 = _plain(build_panel(state), width=84)  # 78 content cols: Model squeezed to 13
    assert _header(at_84) == _RATED_HEADER
    assert _row(at_84, "acme-experim…") == [
        "acme-experim…", "999.9M", "12.3456", "999.9M", "123.4567", "999.9M", "1.23456",
        "$123,456.78",
    ]
    assert "acme-experimental-model" in _plain(build_panel(state), width=100)


def test_squeeze_stops_exactly_at_the_floor_and_keeps_priced_names_whole():
    """The narrowest width that still shows rates squeezes the Model column to exactly
    13 cells — `gpt-5.6-terra`, the longest bundled priced name, still whole — and one
    column narrower the board is the plain one, never a cut priced name."""
    state = _board_state(
        [
            ("gpt-5.6-terra", True, 999_900_000, 999_900_000, 999_900_000, 123_456.78),
            ("codex-unattributed", False, 999_900_000, 999_900_000, 999_900_000, 0.0),
        ]
    )
    theme = get_theme("dark")
    first = next(
        w for w in range(40, 131) if "$/M" in _plain(model_block(state, theme), width=w)
    )
    squeezed = _plain(model_block(state, theme), width=first)
    assert _row(squeezed, "gpt-5.6-terra")[0] == "gpt-5.6-terra"
    assert _row(squeezed, "codex-")[:2] == ["codex-unat…", "*"]  # 13 cells: the floor
    narrower = _plain(model_block(state, theme), width=first - 1)
    assert len(RATE_FOOTNOTE) <= first - 1  # the footnote isn't what drops the rates
    assert _header(narrower) == ["Model", "In", "Out", "Cache", "Cost"]
    assert "gpt-5.6-terra" in narrower and "…" not in narrower


def test_shortened_names_that_would_collide_fall_back_to_the_plain_board():
    """Cutting `gpt-5.1-codex-max` / `gpt-5.1-codex-mini` to a shared prefix would show
    two identical labels (with different rates, if both were priced): keep the plain
    board instead. Wherever rates do show, every Model label is distinct."""
    rows = [
        ("gpt-5.6-sol", True, 91_400_000, 8_000_000, 3_900_000_000, 2639.20),
        ("gpt-5.1-codex-max", False, 40_000_000, 3_000_000, 1_000_000_000, 0.0),
        ("gpt-5.1-codex-mini", False, 9_000_000, 700_000, 300_000_000, 0.0),
    ]
    priced = {
        **BUNDLED,
        "gpt-5.1-codex-max": {"input": 1.25, "output": 10.0, "cache_read": 0.125},
        "gpt-5.1-codex-mini": {"input": 0.25, "output": 2.0, "cache_read": 0.025},
    }
    theme = get_theme("dark")
    for state in (
        _board_state(rows),
        _board_state([(mid, True, *rest) for mid, _known, *rest in rows], pricing=priced),
    ):
        at_70 = _plain(model_block(state, theme), width=70)
        assert _header(at_70) == ["Model", "In", "Out", "Cache", "Cost"]
        assert "gpt-5.1-codex-max" in at_70 and "gpt-5.1-codex-mini" in at_70
        for width in range(40, 131):
            out = _plain(model_block(state, theme), width=width)
            if "$/M" not in out:
                continue
            labels = [_row(out, prefix)[0] for prefix in ("gpt-5.6-sol", "gpt-5.1-codex-ma",
                                                          "gpt-5.1-codex-mi")]
            assert len(set(labels)) == 3, (width, labels)


def test_measurement_matches_what_is_drawn_at_every_width():
    """Textual sizes the Models widget from `__rich_measure__`, then draws at that size.
    The measurement must be exactly the width drawn, and drawing at the measured width
    must give the very same board — rated or plain — as drawing at the offered width."""
    states = [
        _board_state(),
        _board_state(  # short names: here the footnote, not the table, sets the fit
            [
                ("claude-opus-5", True, 15_000, 4_300_000, 1_600_000_000, 1075.69),
                ("claude-fable-5-1", True, 17_000, 1_500_000, 412_900_000, 386.50),
                ("claude-opus-5-5", True, 1_000, 61_000, 100_500_000, 40.16),
            ]
        ),
    ]
    theme = get_theme("dark")
    for state in states:
        for width in range(40, 131):
            board = model_block(state, theme)
            console = Console(file=io.StringIO(), width=width, no_color=True)
            measured = Measurement.get(console, console.options, board).maximum
            drawn = _plain(board, width=width)
            assert max(cell_len(line) for line in drawn.splitlines()) == measured, width
            assert _plain(board, width=measured) == drawn, width


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


def _cache_bill(card):
    """What compute_cost charges for exactly 1M cache-read tokens on `card`."""
    return compute_cost(
        input_tokens=0, output_tokens=0, cache_read=1_000_000, cache_creation_total=0,
        ephemeral_5m=0, ephemeral_1h=0, rates=card,
    )


def test_board_and_cost_engine_share_one_cache_read_helper(monkeypatch):
    """R2: the board has no cache-read math of its own. Change the engine's derivation and
    the board and the bill move together."""
    models = [ModelAgg(model="claude-opus-4-8", input_tokens=1200, output_tokens=300,
                       cache_tokens=8500, cost=4.75)]
    card = get_rates("claude-opus-4-8", BUNDLED)

    monkeypatch.setattr(cost_module, "CACHE_READ_MULT", 0.2)
    out = _plain(model_block(_rated_state(models), get_theme("dark")))
    assert _row(out, "Opus 4.8")[7] == "1.00" == human_rate(_cache_bill(card))

    monkeypatch.setattr(Rates, "cache_read_rate", lambda self, input_mult=1.0: 7.77 * input_mult)
    out = _plain(model_block(_rated_state(models), get_theme("dark")))
    assert _row(out, "Opus 4.8")[7] == "7.77" == human_rate(_cache_bill(card))


def test_explicit_zero_cache_read_is_billed_and_shown_as_zero():
    """`cache_read: 0.0` is a stated rate, not a missing one: no 0.1x input fallback."""
    pricing = {"claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.0}}
    card = get_rates("claude-opus-4-8", pricing)
    assert _cache_bill(card) == 0.0
    models = [ModelAgg(model="claude-opus-4-8", input_tokens=1200, output_tokens=300,
                       cache_tokens=8500, cost=4.75)]
    out = _plain(model_block(_rated_state(models, pricing=pricing), get_theme("dark")))
    assert _row(out, "Opus 4.8")[7] == "0.00"


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
    assert _header(out) == _RATED_HEADER
    assert sum(RATE_FOOTNOTE in line for line in out.splitlines()) == 1


def test_long_unpriced_name_is_shortened_but_keeps_its_marker_at_width_76():
    """A long unpriced id (real data: `codex-unattributed`) can push the rated board past
    70 columns: only the name is cut, its ` *` marker and every number stay whole."""
    state = _rated_state(
        [
            ModelAgg(model="gpt-5.6-sol", input_tokens=91_400_000, output_tokens=8_000_000,
                     cache_tokens=3_900_000_000, cost=12_639.20),
            ModelAgg(model="codex-unattributed", known=False, input_tokens=75_500_000,
                     output_tokens=5_600_000, cache_tokens=2_100_000_000),
        ]
    )
    out = _plain(build_panel(state), width=76)
    assert _header(out) == _RATED_HEADER
    assert _row(out, "gpt-5.6-sol")[1:] == ["91.4M", "5.00", "8.0M", "30.00", "3.9B", "0.50", "$12,639.20"]
    unpriced = _row(out, "codex-unattrib")
    assert unpriced[0].endswith("…") and unpriced[1] == "*"
    assert unpriced[2:] == ["75.5M", "—", "5.6M", "—", "2.1B", "—", "unpriced"]
    assert _row(out, "Total")[1:] == ["166.9M", "13.6M", "6.0B", "$12,639.20"]
    # With room to spare the full id and marker come back.
    assert _row(_plain(build_panel(state), width=100), "codex-unattributed")[1] == "*"


class _OnceEngine:
    """Stands in for Engine in `run_once`: no scan, no network, a fixed snapshot."""

    def __init__(self, config):
        pass

    def scan(self):
        pass

    def refresh_limits(self):
        pass

    def save_cache(self):
        pass

    def sync_ledger(self):
        pass

    def close(self):
        pass

    def snapshot(self):
        return _board_state()


def test_once_renders_to_the_terminal_width(monkeypatch, capsys):
    """`--once` has no compact mode: a narrow terminal must still get a clean board."""
    monkeypatch.setattr(engine_module, "Engine", _OnceEngine)
    for var in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)

    for columns in ("60", "70"):
        monkeypatch.setenv("COLUMNS", columns)
        run_once(Config())
        out = capsys.readouterr().out
        assert all(len(line) <= int(columns) for line in out.splitlines()), columns
        models = _models_section(out)
        assert "$/M" not in models and "…" not in models, columns
        assert "codex-unattributed *" in models and "gpt-5.6-terra" in models, columns

    # At 50 the board is main's, cell for cell (main already squeezes it that narrow).
    monkeypatch.setenv("COLUMNS", "50")
    run_once(Config())
    out = capsys.readouterr().out
    assert all(len(line) <= 50 for line in out.splitlines())
    assert [line.strip() for line in _models_section(out).splitlines()] == [
        line.strip() for line in _pre_t16("full_44").splitlines()
    ]

    monkeypatch.setenv("COLUMNS", "100")
    run_once(Config())
    out = capsys.readouterr().out
    assert _header(out) == _RATED_HEADER and RATE_FOOTNOTE in out
