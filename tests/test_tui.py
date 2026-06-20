"""Interactive TUI flows (T3 R3) driven by Textual's Pilot harness.

Proves the app is operable with arrow keys + Enter only: switch the heartbeat window,
toggle its metric, open Settings, change a setting by arrow+Enter, and quit cleanly.
Hermetic — config writes are redirected to a tmp file; nothing here touches ~/.claude
or the real statusline (that reversibility proof is a separate, explicit script).
"""

from __future__ import annotations

import asyncio

import pytest

import cc_usage.config as cfgmod
from cc_usage.app import CCUsageApp
from cc_usage.config import Config
from cc_usage.engine import Engine


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect config persistence to a tmp file (so Settings changes don't touch HOME)."""
    monkeypatch.setattr(cfgmod, "CONFIG_JSON", tmp_path / "config.json")
    # settings_screen imports save_config from config; patching the module attr is enough
    return tmp_path


def _app() -> CCUsageApp:
    # Empty engine: no transcripts needed to exercise navigation; snapshot still renders
    # (empty windows + flat heartbeat). Avoids depending on real ~/.claude data.
    eng = Engine(Config())
    eng._scanned = True  # skip the disk scan; records stay []
    return CCUsageApp(eng)


def test_heartbeat_window_and_metric_by_keyboard(tmp_config):
    """The heartbeat window (←/→) AND metric (↑/↓) must flip with arrows ALONE.

    The metric is exercised via the *arrow* path (down/up) — not the letter `t` — to
    prove the overriding keyboard-only principle (T3): the user never has to remember a
    letter hotkey. `t` is asserted separately, only as an optional shortcut.
    """

    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            assert app.engine.hb_window == "24h"  # default
            assert app.engine.hb_metric == "cost"
            await pilot.press("right")  # 24h -> 7d
            assert app.engine.hb_window == "7d"
            await pilot.press("right")  # 7d -> 5h (wraps)
            assert app.engine.hb_window == "5h"
            await pilot.press("left")  # 5h -> 7d
            assert app.engine.hb_window == "7d"
            # METRIC via ARROWS ONLY (the Finding-1 path): down flips cost -> tokens.
            await pilot.press("down")  # cost -> tokens
            assert app.engine.hb_metric == "tokens"
            await pilot.press("up")  # tokens -> cost
            assert app.engine.hb_metric == "cost"
            # the heartbeat widget actually rendered something (a Rich renderable)
            from textual.widgets import Static

            hb = app.query_one("#hb", Static)
            assert hb.renderable is not None

    asyncio.run(scenario())


def test_heartbeat_metric_letter_shortcut_still_works(tmp_config):
    """`t` is kept as an optional shortcut for the metric (must not have been removed)."""

    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            assert app.engine.hb_metric == "cost"
            await pilot.press("t")  # cost -> tokens
            assert app.engine.hb_metric == "tokens"
            await pilot.press("t")  # tokens -> cost
            assert app.engine.hb_metric == "cost"

    asyncio.run(scenario())


def test_open_settings_and_change_window_by_arrows(tmp_config):
    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            from cc_usage.settings_screen import ChoiceScreen, SettingsScreen

            await pilot.press("s")  # open Settings
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            metric_before = app.engine.hb_metric

            # Settings list order: refresh, window, cost, theme, statusline.
            # Move down to "Default table window" (index 1) and activate it. The ↓ here
            # must navigate the list, NOT flip the (priority-bound) heartbeat metric.
            await pilot.press("down")  # highlight window row
            assert app.engine.hb_metric == metric_before  # ↓ did not leak to the panel
            await pilot.press("enter")  # open the window picker
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)

            # WINDOW_CHOICES = ["all","1h","5h","24h","7d"]; current "all" at top.
            # Arrow down to "7d" (last) and Enter to select it.
            for _ in range(4):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            # back on the Settings screen, config updated + persisted
            assert isinstance(app.screen, SettingsScreen)
            assert app.config.default_window == "7d"

            # Esc back to the main panel
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)

            # And it persisted to disk
            assert cfgmod.load_config().default_window == "7d"

    asyncio.run(scenario())


def test_change_theme_and_cost_by_keyboard(tmp_config):
    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            from cc_usage.settings_screen import SettingsScreen

            await pilot.press("s")
            await pilot.pause()
            # toggle cost off: row index 2
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")  # open cost picker (on/off)
            await pilot.pause()
            await pilot.press("down")  # highlight "off"
            await pilot.press("enter")
            await pilot.pause()
            assert app.config.show_cost is False
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert cfgmod.load_config().show_cost is False

    asyncio.run(scenario())


def test_settings_highlight_restored_after_pick_without_arrow(tmp_config, monkeypatch):
    """After Enter-selecting in a picker, the Settings list must come back *focused*
    AND visibly highlighted — with NO arrow press. A ListView only paints its cursor
    while focused, so this proves the highlight is restored on screen resume.

    Also covers the statusline ResultScreen pop path (a different push/pop than the
    value pickers) to prove the resume-focus is uniform. That path is kept hermetic:
    statusline status()/install() are stubbed so the real ~/.claude is never touched.
    """
    import cc_usage.settings_screen as ss

    # Hermetic statusline: never read/write ~/.claude. status() drives the menu label
    # and _refresh_list(); install() drives the ResultScreen text we then dismiss.
    monkeypatch.setattr(ss, "status", lambda: {"installed": False})
    monkeypatch.setattr(
        ss, "install", lambda: {"ok": True, "action": "install", "msg": "stubbed"}
    )

    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from cc_usage.settings_screen import (
                ChoiceScreen,
                ResultScreen,
                SettingsScreen,
            )

            await pilot.press("s")  # open Settings
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)

            # Open the window picker (row 1) and Enter-select a value.
            await pilot.press("down")  # highlight "window"
            await pilot.press("enter")  # open the picker
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("enter")  # confirm current value, pop back
            await pilot.pause()

            # Back on Settings — assert WITHOUT pressing any arrow key:
            assert isinstance(app.screen, SettingsScreen)
            lv = app.screen.query_one("#settings-list", ListView)
            # 1) the list regained focus (cursor only paints while focused)
            assert lv.has_focus
            assert app.screen.focused is lv
            # 2) a row is highlighted and that ListItem actually exists
            assert lv.index is not None
            assert lv.highlighted_child is not None
            assert lv.highlighted_child in list(lv.children)
            # 3) THE BUG: the highlighted ListItem must actually carry the
            #    `-highlight` class — that (with focus) is what paints the visible
            #    cursor. Pre-fix the rebuild left it off, so the cursor was invisible
            #    until an arrow press. No arrow was pressed above.
            assert lv.highlighted_child.has_class("-highlight")

            # ── statusline ResultScreen path (Install -> result -> pop) ──
            # Jump to the statusline row (last) and open its menu.
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("down")  # now on "statusline" (index 4)
            await pilot.press("enter")  # statusline menu (a ChoiceScreen)
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("enter")  # choose "Install" (first row) -> ResultScreen
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            await pilot.press("enter")  # dismiss ResultScreen, pop back to Settings
            await pilot.pause()

            assert isinstance(app.screen, SettingsScreen)
            lv = app.screen.query_one("#settings-list", ListView)
            assert lv.has_focus  # restored after ResultScreen too, no arrow press
            assert lv.index is not None
            assert lv.highlighted_child is not None
            assert lv.highlighted_child.has_class("-highlight")

    asyncio.run(scenario())


def test_quit_is_clean(tmp_config):
    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            await pilot.press("q")
        # run_test() exits the context without raising -> clean quit + restore
        assert app.return_code in (0, None)

    asyncio.run(scenario())
