"""Render the panel pieces (T0 §8, T3 R1/R2) as Rich renderables.

Pure functions of a RenderState snapshot -> Rich renderables. The same builders feed
both the Textual TUI (each block hosted in a Static widget) and `--once` (the whole
panel printed once). Reset countdowns are computed from `now` on every call so they
tick down live between data refreshes (Guardrail 2). Whatever rate-limit buckets exist
are rendered; none -> an n/a line (Guardrails 3 & 4).

T3 additions:
  * a **7d** column in the spend block (R1),
  * `heartbeat_renderable()` — the compact braille pulse with a minimal label,
    active metric+window, peak, and span endpoints (R2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .aggregate import RangeAgg, Series, WindowAgg
from .braille import chart_rows
from .config import Config
from .format import human_duration, human_money, human_tokens, pretty_model_name
from .ratelimits import Bucket
from .themes import bar_style, get_theme

_BAR_WIDTH = 14
# T3 R1: 7d sits between 24h and all-time, everywhere windows appear.
_WINDOW_COLS = [("1h", "1h"), ("5h", "5h"), ("24h", "24h"), ("7d", "7d"), ("all", "all-time")]
_WINDOW_LABEL = {
    "1h": "last 1h",
    "5h": "last 5h",
    "24h": "last 24h",
    "7d": "last 7d",
    "all": "all-time",
}


@dataclass
class RenderState:
    windows: dict[str, WindowAgg]
    buckets: list[Bucket]
    now: float
    config: Config
    interval: int
    rl_present: bool = True
    unknown_models: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    heartbeat: Series | None = None  # T3 R2; None only for legacy/one-off paths


def _make_bar(pct: float, theme: dict[str, str]) -> Text:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * _BAR_WIDTH))
    t = Text()
    t.append("▓" * filled, style=bar_style(theme, pct))
    t.append("░" * (_BAR_WIDTH - filled), style=theme["bar_empty"])
    return t


def limits_block(state: RenderState, theme: dict[str, str]):
    if not state.buckets:
        msg = "5h / 7d: n/a — run a Claude Code turn to populate"
        if state.rl_present:
            msg = "limits: n/a — capture present but no usable buckets yet"
        return Text(msg, style=theme["dim"])

    t = Table(box=None, show_header=False, pad_edge=False, expand=False)
    t.add_column(style=theme["label"], no_wrap=True)  # bucket label
    t.add_column(no_wrap=True)  # bar
    t.add_column(justify="right", style=theme["value"], no_wrap=True)  # pct
    t.add_column(style=theme["dim"], no_wrap=True)  # reset
    for b in state.buckets:
        remaining = b.resets_at - state.now
        t.add_row(
            b.label,
            _make_bar(b.used_percentage, theme),
            f"{b.used_percentage:.0f}%",
            f"resets in {human_duration(remaining)}",
        )
    return t


def spend_block(state: RenderState, theme: dict[str, str]):
    t = Table(box=None, pad_edge=False, expand=False)
    t.add_column("Spend", style=theme["label"], no_wrap=True)
    for _key, label in _WINDOW_COLS:
        t.add_column(
            label, justify="right", style=theme["value"], no_wrap=True, header_style=theme["header"]
        )
    t.add_row(
        "tokens", *[human_tokens(state.windows[k].total_tokens) for k, _ in _WINDOW_COLS]
    )
    if state.config.show_cost:
        t.add_row("cost", *[human_money(state.windows[k].cost) for k, _ in _WINDOW_COLS])
    return t


# T4 — the heartbeat is a fixed, taller chart with real axes.
HEARTBEAT_HEIGHT = 8  # H1: plot-body height in character rows (tunable).
_Y_GUTTER = 8  # left-gutter width (chars) reserved for the Y-axis number labels.


def _fmt_metric(value: float, metric: str) -> str:
    """Per-metric value label — cost `$X.XX`, tokens `K`/`M` (H2)."""
    return human_money(value) if metric == "cost" else human_tokens(value)


def _peak_bucket_index(hb: Series) -> int:
    """Index of the bucket holding the peak (first max). 0 for an empty/flat series."""
    if not hb.values:
        return 0
    peak = hb.peak
    for i, v in enumerate(hb.values):
        if v >= peak:
            return i
    return len(hb.values) - 1


def _bucket_center_time(hb: Series, idx: int) -> float:
    """Epoch seconds at the *center* of bucket `idx`.

    Derived from `now`, the window length and the bucket width (H3) — not from any new
    `series()` field, so `series()`'s contract is untouched. Bucket 0 is the oldest edge;
    bucket centers march forward by `bucket_seconds` up to `now`.
    """
    start = hb.now - hb.window_seconds  # left edge of the oldest bucket
    return start + (idx + 0.5) * hb.bucket_seconds


def _x_axis_ticks(hb: Series, n: int = 5) -> list[tuple[float, str]]:
    """`n` evenly spaced X labels as (fraction 0..1 across the window, text).

    Scales the relative-time text to the window: 5h/24h use `-Nh`, 7d uses `-Nd`; the
    rightmost tick is `now`.
    """
    n = max(2, n)
    secs = hb.window_seconds
    use_days = secs >= 2 * 24 * 3600  # 7d window -> day labels read better than hours
    ticks: list[tuple[float, str]] = []
    for k in range(n):
        frac = k / (n - 1)
        ago = secs * (1.0 - frac)  # seconds before `now` at this tick
        if k == n - 1 or ago < 1:
            ticks.append((frac, "now"))
        elif use_days:
            ticks.append((frac, f"-{ago / 86400:.0f}d"))
        else:
            ticks.append((frac, f"-{ago / 3600:.0f}h"))
    return ticks


def _x_axis_line(hb: Series, width: int, gutter: int) -> str:
    """A single text line of X-axis ticks aligned under a `width`-char-wide plot body.

    `gutter` blanks precede the plot so ticks sit under the chart, not the Y labels.
    Labels are placed at their fractional column and never overlap (later labels yield).
    """
    if width <= 0:
        return ""
    slots = [" "] * width
    for frac, label in _x_axis_ticks(hb):
        col = int(round(frac * (width - 1)))
        # Left-anchor most labels; right-anchor the final ("now") so it never overflows.
        start = col if frac < 1.0 else width - len(label)
        start = max(0, min(width - len(label), start))
        if all(slots[start + j] == " " for j in range(len(label))):
            for j, ch in enumerate(label):
                slots[start + j] = ch
    return (" " * gutter) + "".join(slots).rstrip()


def _y_axis_labels(peak: float, metric: str, rows: int) -> list[str]:
    """One right-justified Y label per chart row, top (peak) .. bottom (`0`).

    Peak at the top row, `0` at the bottom, with 1–2 intermediate ticks; blank elsewhere.
    All labels are recomputed from the live `peak` each refresh (H2).
    """
    labels = [""] * rows
    # Tick at top (peak), bottom (0), and ~mid / quarter rows for context.
    tick_rows = {0, rows - 1}
    if rows >= 4:
        tick_rows.add(rows // 2)
    if rows >= 6:
        tick_rows.add(rows - 1 - rows // 4)
    for r in sorted(tick_rows):
        frac = (rows - 1 - r) / (rows - 1) if rows > 1 else 1.0  # row 0 = top = peak
        val = peak * frac
        labels[r] = "0" if r == rows - 1 else _fmt_metric(val, metric)
    width = max((len(s) for s in labels), default=0)
    return [s.rjust(width) for s in labels]


def _peak_annotation(hb: Series) -> str:
    """`peak $14.78/bucket · 14:30 (3h ago)` — value + clock + relative time (H3)."""
    peak_str = _fmt_metric(hb.peak, hb.metric)
    idx = _peak_bucket_index(hb)
    center = _bucket_center_time(hb, idx)
    clock = time.strftime("%H:%M", time.localtime(center))
    ago = max(0.0, hb.now - center)
    rel = "now" if ago < 60 else f"{human_duration(ago)} ago"
    return f"peak {peak_str}/bucket · {clock} ({rel})"


def heartbeat_renderable(state: RenderState, theme: dict[str, str]):
    """The heartbeat as a fixed ~8-row braille chart with real axes (T4).

    H1 fixed height, H2 dynamic Y-axis (peak at top, `0` bottom, value labels), H3 X-axis
    time ticks + a peak-time annotation, H4 unchanged keyboard affordances. An empty window
    renders a flat baseline at 0 with the axes still labeled and `no activity` — never a
    crash; single-bucket / all-equal values are handled (no divide-by-zero).
    """
    hb = state.heartbeat
    if hb is None:
        return Text("heartbeat: n/a", style=theme["dim"])

    metric_name = "cost" if hb.metric == "cost" else "tokens"
    header = Text()
    header.append("heartbeat ", style=theme["label"])
    header.append(f"{metric_name}", style=theme["header"])
    header.append(" · ", style=theme["dim"])
    header.append(f"{hb.window}", style=theme["value"])
    header.append("   (←/→ window · ↑/↓ metric)", style=theme["dim"])

    rows = HEARTBEAT_HEIGHT
    body_rows = chart_rows(hb.values, hb.peak, rows)
    y_labels = _y_axis_labels(hb.peak, hb.metric, rows)
    gutter = max(_Y_GUTTER, max((len(s) for s in y_labels), default=0))
    body_style = theme["dim"] if hb.is_empty else theme["good"]

    plot_width = max((len(s) for s in body_rows), default=0)

    lines: list[Text] = [header]
    for label, glyphs in zip(y_labels, body_rows):
        line = Text()
        line.append(label.rjust(gutter) + " ", style=theme["dim"])
        line.append(glyphs, style=body_style)
        lines.append(line)

    # X-axis ticks (under the plot), then the peak-time / no-activity annotation.
    lines.append(Text(_x_axis_line(hb, plot_width, gutter + 1), style=theme["dim"]))
    if hb.is_empty:
        lines.append(Text((" " * (gutter + 1)) + "no activity", style=theme["dim"]))
    else:
        lines.append(
            Text((" " * (gutter + 1)) + _peak_annotation(hb), style=theme["value"])
        )

    return Group(*lines)


def model_block(state: RenderState, theme: dict[str, str]):
    win_key = state.config.default_window
    win = state.windows.get(win_key) or state.windows["all"]
    show_cost = state.config.show_cost

    t = Table(
        box=None,
        pad_edge=False,
        expand=False,
        title=f"By model · {_WINDOW_LABEL.get(win_key, win_key)}",
        title_justify="left",
        title_style=theme["label"],
    )
    t.add_column("Model", style=theme["model"], no_wrap=True)
    t.add_column("In", justify="right", header_style=theme["header"], style=theme["value"])
    t.add_column("Out", justify="right", header_style=theme["header"], style=theme["value"])
    t.add_column("Cache", justify="right", header_style=theme["header"], style=theme["value"])
    if show_cost:
        t.add_column("Cost", justify="right", header_style=theme["header"], style=theme["value"])

    rows = win.models_sorted()
    if not rows:
        span = 4 + (1 if show_cost else 0)
        t.add_row("no usage in this window", *([""] * (span - 1)), style=theme["dim"])
        return t

    for m in rows:
        name = pretty_model_name(m.model)
        if not m.known:
            name += " *"
        cells = [
            name,
            human_tokens(m.input_tokens),
            human_tokens(m.output_tokens),
            human_tokens(m.cache_tokens),
        ]
        if show_cost:
            cells.append(human_money(m.cost))
        t.add_row(*cells, style=(theme["dim"] if not m.known else None))

    t.add_section()
    total_cells = [
        "Total",
        human_tokens(win.input_tokens),
        human_tokens(win.output_tokens),
        human_tokens(win.cache_tokens),
    ]
    if show_cost:
        total_cells.append(human_money(win.cost))
    t.add_row(*total_cells, style=theme["total"])
    return t


def footnotes(state: RenderState, theme: dict[str, str]) -> list[Text]:
    out: list[Text] = []
    if state.config.show_cost:
        out.append(
            Text(
                "cost = API-equivalent value of tokens · you are on a subscription",
                style=theme["dim"],
            )
        )
    if state.unknown_models:
        names = ", ".join(sorted(state.unknown_models))
        out.append(Text(f"* unknown model(s) priced at $0: {names}", style=theme["warn"]))
    for w in state.warnings:
        out.append(Text(f"⚠ {w}", style=theme["warn"]))
    return out


def build_panel(state: RenderState) -> Panel:
    """Whole-panel renderable for `--once` and any non-Textual path (T0 §8)."""
    theme = get_theme(state.config.theme)
    clock = time.strftime("%H:%M:%S", time.localtime(state.now))

    parts: list = [
        limits_block(state, theme),
        Rule(style=theme["dim"]),
        spend_block(state, theme),
        Rule(style=theme["dim"]),
        heartbeat_renderable(state, theme),
        Rule(style=theme["dim"]),
        model_block(state, theme),
    ]
    notes = footnotes(state, theme)
    if notes:
        parts.append(Text(""))
        parts.extend(notes)

    return Panel(
        Group(*parts),
        title=Text("CC Usage", style=theme["title"]),
        title_align="left",
        subtitle=Text(f"⟳ {state.interval}s · {clock}", style=theme["subtitle"]),
        subtitle_align="right",
        border_style=theme["border"],
        box=box.ROUNDED,
        padding=(1, 2),
        expand=False,
    )


# ── Date-range analysis results (T7) ───────────────────────────────────────────
# Pure renderers of a RangeAgg + theme, mirroring model_block's style, so RangeScreen
# (and any later analysis view) reuses them. The range is the inclusive LOCAL-calendar
# range the user picked; days are zero-filled (see aggregate.aggregate_range).

_RANGE_CHART_ROWS = 5  # braille daily-cost chart height (compact; tables are primary)


def range_header(rng: RangeAgg, theme: dict[str, str]) -> Text:
    """`Usage · 2026-06-13 → 2026-06-20  (8 days)`."""
    t = Text()
    t.append("Usage", style=theme["label"])
    t.append(" · ", style=theme["dim"])
    if rng.days:
        start_label = rng.days[0].date.isoformat()
        end_label = rng.days[-1].date.isoformat()
    else:  # defensive: an inverted/empty span still renders a sane header
        start_label = time.strftime("%Y-%m-%d", time.localtime(rng.start_ts))
        end_label = time.strftime("%Y-%m-%d", time.localtime(rng.end_ts))
    t.append(f"{start_label}", style=theme["value"])
    t.append(" → ", style=theme["dim"])
    t.append(f"{end_label}", style=theme["value"])
    n = rng.n_days
    t.append(f"  ({n} day{'s' if n != 1 else ''})", style=theme["dim"])
    return t


def range_totals_block(rng: RangeAgg, theme: dict[str, str], show_cost: bool = True):
    """Two-column totals: tokens in/out/cache/total, cost, active/total days, records."""
    t = Table(box=None, show_header=False, pad_edge=False, expand=False)
    t.add_column(style=theme["label"], no_wrap=True)
    t.add_column(justify="right", style=theme["value"], no_wrap=True)

    t.add_row("tokens in", human_tokens(rng.input_tokens))
    t.add_row("tokens out", human_tokens(rng.output_tokens))
    t.add_row("cache", human_tokens(rng.cache_tokens))
    t.add_row("total tokens", human_tokens(rng.total_tokens))
    if show_cost:
        t.add_row("cost", human_money(rng.cost))
    t.add_row("active days", f"{rng.active_days} / {rng.n_days}")
    t.add_row("records", str(rng.record_count))
    return t


def range_model_block(rng: RangeAgg, theme: dict[str, str], show_cost: bool = True):
    """By-model table for the range: Model · In · Out · Cache · Cost, cost desc.

    Unknown models are flagged with `*` and contribute $0 (never a crash). An empty
    range shows a single muted `no activity in this range` row.
    """
    t = Table(
        box=None,
        pad_edge=False,
        expand=False,
        title="By model",
        title_justify="left",
        title_style=theme["label"],
    )
    t.add_column("Model", style=theme["model"], no_wrap=True)
    t.add_column("In", justify="right", header_style=theme["header"], style=theme["value"])
    t.add_column("Out", justify="right", header_style=theme["header"], style=theme["value"])
    t.add_column("Cache", justify="right", header_style=theme["header"], style=theme["value"])
    if show_cost:
        t.add_column("Cost", justify="right", header_style=theme["header"], style=theme["value"])

    rows = rng.models_sorted()
    if not rows:
        span = 4 + (1 if show_cost else 0)
        t.add_row("no activity in this range", *([""] * (span - 1)), style=theme["dim"])
        return t

    for m in rows:
        name = pretty_model_name(m.model)
        if not m.known:
            name += " *"
        cells = [
            name,
            human_tokens(m.input_tokens),
            human_tokens(m.output_tokens),
            human_tokens(m.cache_tokens),
        ]
        if show_cost:
            cells.append(human_money(m.cost))
        t.add_row(*cells, style=(theme["dim"] if not m.known else None))

    t.add_section()
    total_cells = [
        "Total",
        human_tokens(rng.input_tokens),
        human_tokens(rng.output_tokens),
        human_tokens(rng.cache_tokens),
    ]
    if show_cost:
        total_cells.append(human_money(rng.cost))
    t.add_row(*total_cells, style=theme["total"])
    return t


def range_day_block(rng: RangeAgg, theme: dict[str, str], show_cost: bool = True):
    """By-day table: Date · Tokens · Cost, chronological, incl. zero days (`–`/`$0.00`).

    Zero days are shown muted with an en-dash for tokens so gaps read at a glance. The
    hosting widget scrolls when the range is long (this just emits all rows).
    """
    t = Table(
        box=None,
        pad_edge=False,
        expand=False,
        title="By day",
        title_justify="left",
        title_style=theme["label"],
    )
    t.add_column("Date", style=theme["model"], no_wrap=True)
    t.add_column("Tokens", justify="right", header_style=theme["header"], style=theme["value"])
    if show_cost:
        t.add_column("Cost", justify="right", header_style=theme["header"], style=theme["value"])

    if not rng.days:
        span = 2 + (1 if show_cost else 0)
        t.add_row("no activity in this range", *([""] * (span - 1)), style=theme["dim"])
        return t

    for day in rng.days:
        active = day.total_tokens > 0 or day.cost > 0.0
        tok = human_tokens(day.total_tokens) if active else "–"
        cells = [day.date.isoformat(), tok]
        if show_cost:
            cells.append(human_money(day.cost))
        t.add_row(*cells, style=(None if active else theme["dim"]))
    return t


def range_chart(rng: RangeAgg, theme: dict[str, str], metric: str = "cost"):
    """A compact braille chart of per-day `metric` across the range, peak labelled.

    Default metric is cost; `tokens` is the alternate. Empty range -> a flat baseline and
    `no activity in this range`, never a crash (reuses the heartbeat's `chart_rows`).
    """
    metric = "cost" if metric not in ("cost", "tokens") else metric
    values = [
        (d.cost if metric == "cost" else float(d.total_tokens)) for d in rng.days
    ]
    peak = max(values) if values else 0.0
    # "Empty" = genuinely NO activity in the range, NOT merely a $0 cost peak. A range with
    # tokens but $0 cost (only unknown/unpriced models) is real activity: it draws a flat
    # bar with a `$0.00` peak label, not "no activity" (F4). Base it on record_count so the
    # cost chart and the tokens chart agree on whether the range is empty.
    is_empty = rng.record_count == 0

    header = Text()
    header.append("Daily ", style=theme["label"])
    header.append(metric, style=theme["header"])
    header.append("   (m: toggle cost/tokens)", style=theme["dim"])

    rows = _RANGE_CHART_ROWS
    body = chart_rows(values, peak, rows) if values else [""] * rows
    body_style = theme["dim"] if is_empty else theme["good"]

    lines: list[Text] = [header]
    for glyphs in body:
        lines.append(Text(glyphs, style=body_style))

    if is_empty:
        lines.append(Text("no activity in this range", style=theme["dim"]))
    else:
        # `values` may be all-zero (tokens-but-$0 range): label the peak as the first day so
        # there's still a definite peak day, and show the $0.00 / 0 peak value honestly.
        peak_idx = max(range(len(values)), key=lambda i: values[i]) if values else 0
        peak_day = rng.days[peak_idx].date.isoformat() if rng.days else "—"
        peak_label = _fmt_metric(peak, metric)
        lines.append(
            Text(f"peak {peak_label} · {peak_day}", style=theme["value"])
        )
    return Group(*lines)
