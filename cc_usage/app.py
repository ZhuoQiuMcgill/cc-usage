"""The interactive Textual TUI (T3 R3) — keyboard-only.

One command (`ccusage`) launches this. The whole app is operable with **arrow keys +
Enter** (plus `q`/Ctrl-C to quit, Esc to back out of Settings):

  * ← / →            switch the heartbeat window (5h / 24h / 7d)
  * ↑ / ↓            toggle the heartbeat metric (cost / tokens); `t` is an extra shortcut
  * s  or  Enter     open Settings (refresh, default window incl. 7d/all-time, show-cost,
                     theme) — itself fully arrow-navigable
  * q / Ctrl-C       quit cleanly; Textual restores the terminal (alt-screen) on exit

Data keeps refreshing on the configured interval via a Textual timer while the UI stays
responsive; a separate 1 s tick re-renders so the reset countdowns and heartbeat move
live. No memorized CLI flags are required for any of this (the overriding T3 principle).
"""

from __future__ import annotations

import threading
import time

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Rule, Static

from .config import Config
from .engine import Engine
from .format import human_duration
from .parser import ScanCancelled, ScanProgress
from .render import (
    account_scope_line,
    by_account_block,
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
        # `a` cycles the account scope (T11). `a` is free — no other app/screen binds
        # it. Hidden in the footer because it's a no-op for single-account users; the
        # on-panel scope line advertises it when it's actually live.
        Binding("a", "account_scope", "Account scope", show=False),
        Binding("left", "hb_prev", "HB ◄ window", show=True, priority=True),
        Binding("right", "hb_next", "HB ► window", show=True, priority=True),
        Binding("up", "hb_metric", "HB metric", show=True, priority=True),
        Binding("down", "hb_metric", "HB metric", show=False, priority=True),
        Binding("t", "hb_metric", "HB metric", show=False),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("c", "cancel_scan", "Cancel scan", show=False),
    ]

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self._data_timer = None
        self._tick_timer = None
        self._limit_timer = None
        self._scan_cancel = threading.Event()
        self._scan_in_progress = False
        self._scan_show_progress = False
        self._progress_last_emit = 0.0
        # Set when a root toggle lands while a scan is in flight: the old worker is
        # cancelled and its completion callback relaunches the scan exactly once.
        self._rescan_pending = False

    @property
    def config(self) -> Config:
        return self.engine.config

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static(id="scope", classes="block")
            yield Static(id="limits", classes="block")
            yield Rule()
            yield Static(id="spend", classes="block")
            yield Rule()
            yield Static(id="hb", classes="block")
            yield Rule()
            yield Static(id="models", classes="block")
            yield Static(id="accounts", classes="block")
            yield Static(id="notes", classes="block")
        yield Footer()

    def on_mount(self) -> None:
        # Warm caches render immediately; cold scans expose progress and cancellation.
        if self.engine.is_scanned:
            self.render_panel()
            self._start_timers()
        elif self.engine.prime_cache():
            self._start_background_scan(show_progress=False)
            self.render_panel()
        else:
            self._render_scanning()
            self._start_background_scan(show_progress=True)

    def _render_scanning(self) -> None:
        """Paint the initial discovery state without touching the engine."""
        self._update_scan_status("discovering transcripts…  ·  c cancel")

    def _update_scan_status(self, message: str) -> None:
        base = self.screen_stack[0] if self.screen_stack else None
        if base is None:
            return
        try:
            base.query_one("#spend", Static).update(Text(message, style="dim"))
        except Exception:
            return

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} TB"

    def _render_scan_progress(self, update: ScanProgress) -> None:
        if update.phase == "discovering":
            self._update_scan_status("discovering transcripts…  ·  c cancel")
            return
        if update.phase != "parsing":
            return
        if update.bytes_total > 0:
            fraction = min(1.0, update.bytes_done / update.bytes_total)
        elif update.files_total > 0:
            fraction = min(1.0, update.files_done / update.files_total)
        else:
            fraction = 1.0
        narrow = self.size.width < 76
        width = 12 if narrow else 24
        filled = min(width, int(fraction * width))
        bar = "█" * filled + "░" * (width - filled)
        counts = (
            f"{update.files_done:,}/{update.files_total:,} files  ·  "
            f"{self._format_bytes(update.bytes_done)}/{self._format_bytes(update.bytes_total)}"
        )
        if narrow:
            message = f"scan  {bar}  {fraction:>4.0%}\n{counts}  ·  c cancel"
        else:
            message = f"scanning transcripts  {bar}  {fraction:>5.0%}  ·  {counts}  ·  c cancel"
        self._update_scan_status(message)

    def _start_background_scan(self, *, show_progress: bool) -> None:
        if self._scan_in_progress:
            return
        self._scan_cancel.clear()
        self._scan_in_progress = True
        self._scan_show_progress = show_progress
        self._progress_last_emit = 0.0
        self.run_worker(self._background_scan, thread=True, exclusive=True)

    def _background_scan(self) -> None:
        """Heavy scan worker with throttled UI progress and resumable cancellation."""

        def report(update: ScanProgress) -> None:
            if not self._scan_show_progress:
                return
            now = time.monotonic()
            if update.phase == "parsing" and now - self._progress_last_emit < 0.1:
                return
            self._progress_last_emit = now
            self.call_from_thread(self._render_scan_progress, update)

        try:
            self.engine.scan(progress=report, cancelled=self._scan_cancel.is_set)
        except ScanCancelled:
            self.call_from_thread(self._on_scan_cancelled)
            return
        self.engine.save_cache()
        self.engine.refresh_limits()
        self.call_from_thread(self._on_initial_scan_done)

    def _consume_pending_rescan(self) -> bool:
        """Relaunch the queued root-toggle rescan once the previous worker is dead."""
        if not self._rescan_pending:
            return False
        self._rescan_pending = False
        self._render_scanning()
        self._start_background_scan(show_progress=True)
        return True

    def _on_scan_cancelled(self) -> None:
        self._scan_in_progress = False
        if self._consume_pending_rescan():
            return
        if self.engine.is_scanned:
            self.render_panel()
            self._start_timers()
        else:
            self._update_scan_status("scan cancelled  ·  r resume  ·  q quit")

    def _on_initial_scan_done(self) -> None:
        self._scan_in_progress = False
        if self._consume_pending_rescan():
            return
        self.render_panel()
        self._start_timers()
    def _start_timers(self) -> None:
        # Cancel any prior timers (used when the refresh interval changes live).
        if self._data_timer is not None:
            self._data_timer.stop()
        if self._tick_timer is None:
            # 1 s redraw keeps countdowns + heartbeat moving between data refreshes.
            self._tick_timer = self.set_interval(1.0, self.render_panel)
        if self._limit_timer is None:
            self._limit_timer = self.set_interval(300.0, self._refresh_limits)
        self._data_timer = self.set_interval(
            float(self.config.refresh_interval), self._refresh_data
        )

    # ── data / render ────────────────────────────────────────────────────────
    def _refresh_data(self) -> None:
        if self._scan_in_progress or not self.engine.is_scanned:
            # A worker scan is in flight (or a root toggle just reset the data set):
            # a second concurrent scan of the same parser is not safe. The worker's
            # completion callback repaints and restarts the cadence.
            return
        self.engine.scan()
        self.render_panel()

    def _refresh_limits(self) -> None:
        self.run_worker(self._background_limit_refresh, thread=True, exclusive=True)

    def _background_limit_refresh(self) -> None:
        self.engine.refresh_limits()
        self.call_from_thread(self.render_panel)

    def render_panel(self) -> None:
        # The panel widgets live on the base (main) screen. Query *that* screen, not the
        # active top-of-stack, so a refresh tick while Settings is open doesn't fail.
        base = self.screen_stack[0] if self.screen_stack else None
        if base is None:
            return
        if not self.engine.is_scanned:
            # A (re)scan owns the panel right now — the status line is showing its
            # progress, and snapshot() would otherwise trigger a synchronous scan on
            # the UI thread, racing the worker on the same parser.
            return
        try:
            state = self.engine.snapshot()
            state.compact = self.size.width < 76
            theme = get_theme(self.config.theme)
            scope = account_scope_line(state, theme)
            base.query_one("#scope", Static).update(scope if scope is not None else Text(""))
            base.query_one("#limits", Static).update(limits_block(state, theme))
            base.query_one("#spend", Static).update(spend_block(state, theme))
            base.query_one("#hb", Static).update(heartbeat_renderable(state, theme))
            base.query_one("#models", Static).update(model_block(state, theme))
            accounts = by_account_block(state, theme)
            base.query_one("#accounts", Static).update(
                accounts if accounts is not None else Text("")
            )
            notes = footnotes(state, theme)
            base.query_one("#notes", Static).update(Group(*notes) if notes else Text(""))
            if self._scan_in_progress:
                freshness = "reconciling"
            elif self.engine.last_scan_at is None:
                freshness = "ready"
            else:
                age = max(0.0, time.time() - self.engine.last_scan_at)
                freshness = "updated just now" if age < 60 else f"updated {human_duration(age)} ago"
            self.sub_title = f"{freshness} · auto {self.config.refresh_interval}s"
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
        {
            "hb_prev",
            "hb_next",
            "hb_metric",
            "account_scope",
            "date_range",
            "settings",
            "refresh_now",
            "cancel_scan",
        }
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

    def action_account_scope(self) -> None:
        # Cycle all -> each Claude account -> all (no-op for single-account users).
        self.engine.cycle_account_scope()
        self.render_panel()

    def action_refresh_now(self) -> None:
        if not self.engine.is_scanned:
            self._render_scanning()
            self._start_background_scan(show_progress=True)
        else:
            self._refresh_data()

    def action_cancel_scan(self) -> None:
        if self._scan_in_progress:
            self._scan_cancel.set()
            if self._scan_show_progress:
                self._update_scan_status("cancelling scan…")
    def action_settings(self) -> None:
        # Re-render on return so any changed config/theme shows immediately.
        self.push_screen(SettingsScreen(self.config, self.engine), lambda _=None: self._on_settings_closed())

    def _on_settings_closed(self) -> None:
        """Apply Settings changes on return. A toggled account root changes the data
        set, so rebuild the parser and rescan (with progress, off the UI thread);
        anything else is just a live re-render + cadence reset."""
        if self.engine.reload_roots():
            self._render_scanning()
            self._request_rescan()
        else:
            self.apply_config()

    def _request_rescan(self) -> None:
        """Launch a fresh scan for a changed root set. If a scan is already in flight
        it is scanning the swapped-out parser: cancel it and queue the relaunch — the
        worker's completion callback starts the new scan exactly once (the engine's
        generation guard makes the stale worker discard its result either way)."""
        if self._scan_in_progress:
            self._rescan_pending = True
            self._scan_cancel.set()
        else:
            self._start_background_scan(show_progress=True)

    def action_date_range(self) -> None:
        # The date-range analysis screen (T7). Re-render the main panel on return so it
        # reflects any data refresh that ticked while the screen was open. The heartbeat
        # arrows are already gated by check_action() while this (non-base) screen is on top.
        if self._scan_in_progress or not self.engine.is_scanned:
            # RangeScreen computes synchronously through engine.ensure_scanned():
            # opened mid-(re)scan it would run a second scan of the same parser on
            # the UI thread, racing the worker (same guard as _refresh_data /
            # render_panel). The scanning/cancelled status line already says why.
            return
        self.push_screen(RangeScreen(self.engine), lambda _=None: self.render_panel())

    def action_quit(self) -> None:
        self._rescan_pending = False  # never relaunch a scan during teardown
        self._scan_cancel.set()
        self.exit()

def run_tui(config: Config) -> None:
    """Launch the interactive TUI (default `ccusage`)."""
    CCUsageApp(Engine(config)).run()
