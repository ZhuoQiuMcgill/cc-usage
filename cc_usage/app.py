"""The interactive Textual TUI (T3 R3) — keyboard-only.

One command (`ccusage`) launches this. The whole app is operable with **arrow keys +
Enter** (plus `q`/Ctrl-C to quit, Esc to back out of Settings):

  * ← / →            switch the heartbeat window (5h / 24h / 7d)
  * ↑ / ↓            toggle the heartbeat metric (cost / tokens); `t` is an extra shortcut
  * s  or  Enter     open Settings (refresh, default window incl. 7d/all-time, show-cost,
                     theme, statusline install/restore) — itself fully arrow-navigable
  * q / Ctrl-C       quit cleanly; Textual restores the terminal (alt-screen) on exit

Data keeps refreshing on the configured interval via a Textual timer while the UI stays
responsive; a separate 1 s tick re-renders so the reset countdowns and heartbeat move
live. No memorized CLI flags are required for any of this (the overriding T3 principle).
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Rule, Static

from .config import Config
from .engine import Engine
from .render import (
    footnotes,
    heartbeat_renderable,
    limits_block,
    model_block,
    spend_block,
)
from .range_screen import RangeScreen
from .settings_screen import SettingsScreen
from .themes import get_theme

_CSS = """
Screen { align: center top; }
#panel { width: auto; max-width: 100%; height: auto; padding: 1 2; }
.block { width: auto; height: auto; }
#hb { padding: 0 0; }
Rule { color: $panel-darken-2; }

/* Settings + pickers + date-range screen + date stepper */
#settings-box, #choice-box, #result-box, #range-box, #stepper-box {
    width: auto; min-width: 48; max-width: 90%; height: auto;
    border: round $accent; padding: 1 2; background: $panel;
}
#settings-title, #choice-title, #result-text, #range-title, #stepper-title {
    text-style: bold; padding-bottom: 1;
}
#settings-hint, #choice-hint, #result-hint, #range-hint, #stepper-hint {
    color: $text-muted; padding-top: 1;
}
#settings-list, #choices, #range-controls { height: auto; max-height: 16; }
/* The results area scrolls so a long by-day table never overflows the screen. */
#range-results { height: auto; max-height: 60vh; padding-top: 1; }
#stepper-date { padding: 1 0; }
ListView { background: $panel; }
ListView > ListItem { padding: 0 1; }
ListView > ListItem.--highlight { background: $accent; color: $text; }
"""


class CCUsageApp(App):
    TITLE = "CC Usage"
    CSS = _CSS

    # All keyboard-only. Arrows/Enter drive Settings + lists natively.
    # The heartbeat arrows are `priority=True` so they fire on the main panel even though
    # the focused VerticalScroll also binds the arrow keys to scrolling — the user must be
    # able to flip the window/metric with arrows alone, regardless of terminal size.
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("s", "settings", "Settings", show=True),
        Binding("enter", "settings", "Settings", show=True),
        Binding("d", "date_range", "Date range", show=True),
        Binding("left", "hb_prev", "HB ◄ window", show=True, priority=True),
        Binding("right", "hb_next", "HB ► window", show=True, priority=True),
        Binding("up", "hb_metric", "HB metric", show=True, priority=True),
        Binding("down", "hb_metric", "HB metric", show=False, priority=True),
        Binding("t", "hb_metric", "HB metric", show=False),
        Binding("r", "refresh_now", "Refresh", show=False),
    ]

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self._data_timer = None
        self._tick_timer = None

    @property
    def config(self) -> Config:
        return self.engine.config

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static(id="limits", classes="block")
            yield Rule()
            yield Static(id="spend", classes="block")
            yield Rule()
            yield Static(id="hb", classes="block")
            yield Rule()
            yield Static(id="models", classes="block")
            yield Static(id="notes", classes="block")
        yield Footer()

    def on_mount(self) -> None:
        # Background full scan, then render + start the timers.
        self.engine.scan()
        self.render_panel()
        self._start_timers()

    def _start_timers(self) -> None:
        # Cancel any prior timers (used when the refresh interval changes live).
        if self._data_timer is not None:
            self._data_timer.stop()
        if self._tick_timer is None:
            # 1 s redraw keeps countdowns + heartbeat moving between data refreshes.
            self._tick_timer = self.set_interval(1.0, self.render_panel)
        self._data_timer = self.set_interval(
            float(self.config.refresh_interval), self._refresh_data
        )

    # ── data / render ────────────────────────────────────────────────────────
    def _refresh_data(self) -> None:
        self.engine.scan()
        self.render_panel()

    def render_panel(self) -> None:
        # The panel widgets live on the base (main) screen. Query *that* screen, not the
        # active top-of-stack, so a refresh tick while Settings is open doesn't fail.
        base = self.screen_stack[0] if self.screen_stack else None
        if base is None:
            return
        try:
            state = self.engine.snapshot()
            theme = get_theme(self.config.theme)
            base.query_one("#limits", Static).update(limits_block(state, theme))
            base.query_one("#spend", Static).update(spend_block(state, theme))
            base.query_one("#hb", Static).update(heartbeat_renderable(state, theme))
            base.query_one("#models", Static).update(model_block(state, theme))
            notes = footnotes(state, theme)
            base.query_one("#notes", Static).update(Group(*notes) if notes else Text(""))
            self.sub_title = f"⟳ {self.config.refresh_interval}s"
        except Exception:  # never crash the UI on a transient data hiccup / mid-mount
            return

    def apply_config(self) -> None:
        """Called by the Settings screen after a change: re-render + reset cadence."""
        self._start_timers()
        self.render_panel()

    # ── actions (keyboard) ───────────────────────────────────────────────────
    # Every App-level binding that drives the MAIN PANEL (or pushes a screen). Each must be
    # disabled whenever a sub-screen is on top, or its key leaks INTO that screen:
    #   * the heartbeat arrows are `priority=True`, so they'd steal ↑/↓/←/→ from a list/stepper;
    #   * `date_range` (`d`) would push a SECOND RangeScreen on top of the first;
    #   * `settings` (`s`) would stack Settings on top of RangeScreen;
    #   * `refresh_now` (`r`) would fire a panel refresh from inside a sub-screen.
    # (`enter` also maps to `settings`, but a sub-screen's own `enter` binding wins, so it
    # never reaches the App action while a screen is up.) Quit stays available everywhere.
    _PANEL_ONLY_ACTIONS = frozenset(
        {"hb_prev", "hb_next", "hb_metric", "date_range", "settings", "refresh_now"}
    )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate every panel-only App action to the base panel.

        Disabling these whenever anything is pushed on top of the base panel lets the key
        fall through to the focused widget / top screen (Textual continues the priority
        chain when an action is disabled), so no base-app key leaks into a sub-screen."""
        if action in self._PANEL_ONLY_ACTIONS and len(self.screen_stack) > 1:
            return False
        return True

    def action_hb_prev(self) -> None:
        self.engine.cycle_hb_window(-1)
        self.render_panel()

    def action_hb_next(self) -> None:
        self.engine.cycle_hb_window(1)
        self.render_panel()

    def action_hb_metric(self) -> None:
        self.engine.toggle_hb_metric()
        self.render_panel()

    def action_refresh_now(self) -> None:
        self._refresh_data()

    def action_settings(self) -> None:
        # Re-render on return so any changed config/theme shows immediately.
        self.push_screen(SettingsScreen(self.config), lambda _=None: self.apply_config())

    def action_date_range(self) -> None:
        # The date-range analysis screen (T7). Re-render the main panel on return so it
        # reflects any data refresh that ticked while the screen was open. The heartbeat
        # arrows are already gated by check_action() while this (non-base) screen is on top.
        self.push_screen(RangeScreen(self.engine), lambda _=None: self.render_panel())

    def action_quit(self) -> None:
        self.exit()


def run_tui(config: Config) -> None:
    """Launch the interactive TUI (default `ccusage`)."""
    CCUsageApp(Engine(config)).run()
