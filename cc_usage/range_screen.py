"""Date-range usage analysis (T7) — a keyboard-only full screen.

Opened from the main panel with `d`. The user picks a start and end **date** using only
arrow keys + Enter (no typing, no CLI flags) and sees usage metrics for that inclusive
LOCAL-calendar range: totals, a by-model table, a by-day table, and a daily-cost chart.

Layout:
  * Top — a controls ListView [Preset, Start date, End date]; ↑/↓ move, Enter activates,
    Esc/q returns to the main panel. It mirrors settings_screen's ChoiceScreen highlight
    handling, incl. the on_screen_resume/_focus_list net, so the cursor is visible
    immediately on entry and after any sub-screen pops (the highlight bug we fixed before).
  * Bottom — a live results area (Static widgets) that recomputes via engine.range_metrics
    and redraws whenever the range changes.

Sub-screens:
  * Preset  -> the existing ChoiceScreen (Last 7 days / 30 / This month / Last month /
    All time / Custom…). A non-custom preset sets both dates; Custom… leaves them editable.
  * Start/End -> DateStepperScreen, a tiny modal stepper: ↑/↓ ±1 day, ←/→ ±1 month,
    PageUp/PageDown ±1 year, Enter confirm, Esc cancel. Dates clamp to
    [earliest record day … today]; editing one bound past the other auto-corrects the
    other so start<=end always holds.

Timezone: a date range is a human CALENDAR concept, so this view works in LOCAL time —
the picked dates become epoch bounds via aggregate.day_start_ts / day_end_ts, and per-day
bucketing uses each record's local civil day. (Rolling windows elsewhere stay
timezone-independent epoch math; this is the deliberate exception, called out in
aggregate.py too.)
"""

from __future__ import annotations

import calendar
import datetime

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from .aggregate import RangeAgg, day_end_ts, day_start_ts
from .engine import Engine
from .render import (
    range_chart,
    range_day_block,
    range_header,
    range_model_block,
    range_totals_block,
)
from .settings_screen import ChoiceScreen  # the generic arrow-key picker, reused for presets
from .themes import get_theme

# A sane floor for clamping when there are no records at all (so the stepper still works).
_NO_DATA_FLOOR_DAYS = 365


def _today() -> datetime.date:
    """Today's LOCAL calendar date."""
    return datetime.date.today()


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """Shift `d` by `months`, clamping the day to the target month's length."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # Clamp day (e.g. Jan 31 -> Feb 28/29).
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(d.day, last))


def _add_years(d: datetime.date, years: int) -> datetime.date:
    return _add_months(d, years * 12)


def _relative_label(d: datetime.date) -> str:
    """`(today)` / `(7 days ago)` / `(in 3 days)` relative to today."""
    delta = (d - _today()).days
    if delta == 0:
        return "(today)"
    if delta < 0:
        n = -delta
        return f"({n} day{'s' if n != 1 else ''} ago)"
    return f"(in {delta} day{'s' if delta != 1 else ''})"


# Preset values for the ChoiceScreen. The label is what the user sees; the screen turns
# the chosen key into a (start_date, end_date) pair (or leaves dates as-is for "custom").
PRESETS: list[tuple[str, str]] = [
    ("last7", "Last 7 days"),
    ("last30", "Last 30 days"),
    ("thismonth", "This month"),
    ("lastmonth", "Last month"),
    ("all", "All time"),
    ("custom", "Custom…"),
]
_PRESET_LABEL = dict(PRESETS)


class DateStepperScreen(ModalScreen):
    """A tiny keyboard date stepper — no typing.

    ↑/↓ = ±1 day, ←/→ = ±1 month, PageUp/PageDown = ±1 year. Enter confirms (dismisses
    with the chosen `datetime.date`); Esc cancels (dismisses with None). The candidate is
    always clamped to [floor … ceil] so it can't leave the allowed window.
    """

    BINDINGS = [
        Binding("up", "step(1)", "+1 day", show=False),
        Binding("down", "step(-1)", "-1 day", show=False),
        Binding("right", "step_month(1)", "+1 month", show=False),
        Binding("left", "step_month(-1)", "-1 month", show=False),
        Binding("pageup", "step_year(1)", "+1 year", show=False),
        Binding("pagedown", "step_year(-1)", "-1 year", show=False),
        Binding("enter", "confirm", "Confirm", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        title: str,
        value: datetime.date,
        floor: datetime.date,
        ceil: datetime.date,
        theme: dict[str, str],
    ) -> None:
        super().__init__()
        self._title = title
        self._floor = floor
        self._ceil = ceil
        self._theme = theme
        self._value = self._clamp(value)

    def _clamp(self, d: datetime.date) -> datetime.date:
        if d < self._floor:
            return self._floor
        if d > self._ceil:
            return self._ceil
        return d

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="stepper-box"):
                yield Static(self._title, id="stepper-title")
                yield Static(id="stepper-date")
                yield Static(
                    "↑/↓ ±1 day · ←/→ ±1 month · PgUp/PgDn ±1 year · ↵ ok · Esc cancel",
                    id="stepper-hint",
                )

    def on_mount(self) -> None:
        self.focus()
        self._render_date()

    def _render_date(self) -> None:
        t = Text()
        t.append(self._value.isoformat(), style=self._theme["title"])
        t.append("  ", style=self._theme["dim"])
        t.append(_relative_label(self._value), style=self._theme["dim"])
        self.query_one("#stepper-date", Static).update(t)

    def action_step(self, days: int) -> None:
        self._value = self._clamp(self._value + datetime.timedelta(days=days))
        self._render_date()

    def action_step_month(self, months: int) -> None:
        self._value = self._clamp(_add_months(self._value, months))
        self._render_date()

    def action_step_year(self, years: int) -> None:
        self._value = self._clamp(_add_years(self._value, years))
        self._render_date()

    def action_confirm(self) -> None:
        self.dismiss(self._value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _ControlRow(ListItem):
    """One selectable control row that carries its key (preset/start/end)."""

    def __init__(self, value: str, text: str) -> None:
        super().__init__(Label(text))
        self.value = value


class RangeScreen(Screen):
    """The full date-range analysis screen. Keyboard-only, live results."""

    BINDINGS = [
        Binding("escape", "back", "Back to panel", show=True),
        Binding("q", "back", "Back", show=False),
        Binding("m", "toggle_metric", "Chart metric", show=True),
    ]

    # Control rows in the top ListView.
    _CONTROLS = (("preset", "Preset"), ("start", "Start date"), ("end", "End date"))

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self.theme_dict = get_theme(engine.config.theme)
        # Default range on open: Last 7 days (so there's immediately something to see).
        today = _today()
        self.end_date = today
        self.start_date = today - datetime.timedelta(days=6)  # 7 days inclusive
        self.preset_key = "last7"
        self.chart_metric = "cost"
        # NB: don't touch the engine here — computing the earliest-record floor would force
        # a synchronous disk scan on the main thread before mount (F6). The default Last-7
        # range needs no floor; we only clamp the *end* to today, which any preset already
        # respects. The earliest-record floor is computed lazily in _floor() (used solely as
        # the stepper's step-DOWN limit), by which point the app has long since scanned.
        self._clamp_end_to_today()

    # ── allowed-date window ───────────────────────────────────────────────────
    def _earliest_record_date(self) -> datetime.date:
        """Local calendar day of the earliest record (a sane floor if there are none).

        Used ONLY as the stepper's step-down limit and as the "All time" preset start — it
        must NOT retroactively snap an already-earlier preset start (e.g. "Last 30 days" on
        a machine with only 4 days of data still spans the literal 30 calendar days)."""
        self.engine.ensure_scanned()
        records = self.engine.parser.records
        if not records:
            return _today() - datetime.timedelta(days=_NO_DATA_FLOOR_DAYS)
        earliest_ts = min(r.ts for r in records)
        return datetime.datetime.fromtimestamp(earliest_ts).date()

    def _floor(self) -> datetime.date:
        return self._earliest_record_date()

    def _ceil(self) -> datetime.date:
        return _today()

    def _clamp_end_to_today(self) -> None:
        """Keep end ≤ today and start ≤ end. Deliberately does NOT floor-clamp the start:
        a preset reflects its LITERAL calendar span (zero-days in it are fine — they're
        zero-filled). The earliest-record floor lives only on the stepper's step-down."""
        ceil = self._ceil()
        if self.end_date > ceil:
            self.end_date = ceil
        if self.start_date > ceil:
            self.start_date = ceil
        if self.start_date > self.end_date:
            self.start_date = self.end_date

    # ── layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="range-box"):
                yield Static("Date-range analysis", id="range-title")
                yield ListView(id="range-controls")
                yield Static(
                    "↑/↓ move · ↵ change · m chart metric · Esc back to panel",
                    id="range-hint",
                )
                with VerticalScroll(id="range-results"):
                    yield Static(id="range-header", classes="block")
                    yield Static(id="range-totals", classes="block")
                    yield Static(id="range-models", classes="block")
                    yield Static(id="range-chart", classes="block")
                    yield Static(id="range-days", classes="block")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh_controls()
        self._render_results()

    def on_screen_resume(self) -> None:
        # A sub-screen (Preset picker or DateStepper) just popped. Its pop-callback already
        # rebuilt the controls + results, but a ListView only paints its highlight while
        # focused and the pop doesn't return focus to our list — so re-focus + re-assert
        # the highlight here (the settings-screen fix; do not regress). call_after_refresh
        # defends against the pop's own focus restoration landing after this handler.
        self.call_after_refresh(self._focus_list)

    # ── controls list (mirrors settings_screen highlight handling) ─────────────
    def _control_value(self, key: str) -> str:
        if key == "preset":
            return _PRESET_LABEL.get(self.preset_key, self.preset_key)
        if key == "start":
            return self.start_date.isoformat()
        return self.end_date.isoformat()

    async def _refresh_controls(self, keep_index: int | None = None) -> None:
        lv = self.query_one("#range-controls", ListView)
        idx = lv.index if keep_index is None else keep_index
        if idx is None:
            idx = 0
        # AWAIT clear + mounts so children are settled before setting the index — else
        # watch_index toggles `-highlight` onto still-mounting items and the cursor stays
        # invisible until an arrow press (the settings highlight bug). Same fix here.
        await lv.clear()
        rows = [
            _ControlRow(key, f"{label:<12}▸ {self._control_value(key)}")
            for key, label in self._CONTROLS
        ]
        await lv.extend(rows)
        lv.index = max(0, min(idx, len(lv) - 1))
        self._focus_list()

    def _focus_list(self) -> None:
        """Focus the controls list and force its highlight cursor to (re)paint."""
        lv = self.query_one("#range-controls", ListView)
        lv.focus()
        if len(lv) == 0:
            return
        idx = 0 if lv.index is None else max(0, min(lv.index, len(lv) - 1))
        lv.index = idx
        child = lv.highlighted_child
        if child is not None:
            child.highlighted = True

    def _refresh_controls_soon(self, keep_index: int | None = None) -> None:
        """Schedule the async controls rebuild from a sync (pop-callback) context."""
        self.run_worker(self._refresh_controls(keep_index), exclusive=True)

    # ── results ────────────────────────────────────────────────────────────────
    def _compute_range(self) -> RangeAgg:
        start_ts = day_start_ts(self.start_date)
        end_ts = day_end_ts(self.end_date)
        return self.engine.range_metrics(start_ts, end_ts)

    def _render_results(self) -> None:
        try:
            rng = self._compute_range()
            theme = self.theme_dict
            show_cost = self.engine.config.show_cost
            self.query_one("#range-header", Static).update(range_header(rng, theme))
            self.query_one("#range-totals", Static).update(
                range_totals_block(rng, theme, show_cost)
            )
            self.query_one("#range-models", Static).update(
                range_model_block(rng, theme, show_cost)
            )
            self.query_one("#range-chart", Static).update(
                range_chart(rng, theme, self.chart_metric)
            )
            self.query_one("#range-days", Static).update(
                range_day_block(rng, theme, show_cost)
            )
        except NoMatches:
            # The widgets aren't mounted yet (a redraw raced mount). Harmless: on_mount
            # renders once the tree is up. Re-raising would crash the screen for nothing.
            return
        except Exception as exc:
            # Don't silently swallow a real render/aggregation regression (F5) — keep the
            # screen alive, but make the failure VISIBLE in the header so it isn't invisible.
            self.log.error(f"RangeScreen render failed: {exc!r}")
            try:
                self.query_one("#range-header", Static).update(
                    Text(f"render error: {exc}", style=self.theme_dict["warn"])
                )
            except Exception:
                pass

    # ── interactions ───────────────────────────────────────────────────────────
    @on(ListView.Selected, "#range-controls")
    def _row_selected(self, event: ListView.Selected) -> None:
        key = event.item.value  # type: ignore[attr-defined]
        keep = self.query_one("#range-controls", ListView).index
        if key == "preset":
            self._open_preset(keep)
        elif key == "start":
            self._open_stepper("start", keep)
        elif key == "end":
            self._open_stepper("end", keep)

    def _open_preset(self, keep) -> None:
        def done(value) -> None:
            if value is not None:
                self._apply_preset(value)
                self._render_results()
            self._refresh_controls_soon(keep)

        self.app.push_screen(
            ChoiceScreen("Preset range", list(PRESETS), self.preset_key), done
        )

    def _apply_preset(self, key: str) -> None:
        self.preset_key = key
        today = _today()
        if key == "last7":
            self.end_date, self.start_date = today, today - datetime.timedelta(days=6)
        elif key == "last30":
            self.end_date, self.start_date = today, today - datetime.timedelta(days=29)
        elif key == "thismonth":
            self.end_date = today
            self.start_date = today.replace(day=1)
        elif key == "lastmonth":
            first_this = today.replace(day=1)
            last_prev = first_this - datetime.timedelta(days=1)
            self.start_date = last_prev.replace(day=1)
            self.end_date = last_prev
        elif key == "all":
            # The one preset that legitimately uses the earliest record as its start.
            self.start_date = self._earliest_record_date()
            self.end_date = today
        elif key == "custom":
            return  # leave the current dates editable; just mark the preset as custom
        # Only clamp the END to today (+ keep start<=end). Do NOT floor-clamp the start, so
        # "Last 7/30 days", "This month", "Last month" keep their LITERAL calendar span even
        # when data starts later — the missing leading days are simply zero (F3).
        self._clamp_end_to_today()

    def _open_stepper(self, which: str, keep) -> None:
        floor, ceil = self._floor(), self._ceil()
        current = self.start_date if which == "start" else self.end_date
        title = "Start date" if which == "start" else "End date"

        def done(value) -> None:
            if value is not None:
                self._apply_stepper(which, value)
                self._render_results()
            self._refresh_controls_soon(keep)

        self.app.push_screen(
            DateStepperScreen(title, current, floor, ceil, self.theme_dict), done
        )

    def _apply_stepper(self, which: str, value: datetime.date) -> None:
        # Any manual edit makes the range "Custom" (it no longer matches a named preset).
        self.preset_key = "custom"
        if which == "start":
            self.start_date = value
            if self.start_date > self.end_date:
                self.end_date = self.start_date  # auto-correct the other bound
        else:
            self.end_date = value
            if self.end_date < self.start_date:
                self.start_date = self.end_date
        # The stepper already clamped `value` to [floor, ceil], and the auto-corrected
        # bound copies an in-bounds value, so this just re-asserts end<=today + start<=end.
        self._clamp_end_to_today()

    def action_toggle_metric(self) -> None:
        self.chart_metric = "tokens" if self.chart_metric == "cost" else "cost"
        self._render_results()

    def action_back(self) -> None:
        self.dismiss(None)
