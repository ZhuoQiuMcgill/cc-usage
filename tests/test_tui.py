"""Interactive TUI flows (T3 R3) driven by Textual's Pilot harness.

Proves the app is operable with arrow keys + Enter only: switch the heartbeat window,
toggle its metric, open Settings, change a setting by arrow+Enter, and quit cleanly.
Hermetic — config writes are redirected to a tmp file; nothing here touches ~/.claude
or the real statusline (that reversibility proof is a separate, explicit script).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time

import pytest

import cc_usage.config as cfgmod
from cc_usage.app import CCUsageApp
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.parser import UsageRecord


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect config persistence to a tmp file (so Settings changes don't touch HOME)."""
    monkeypatch.setattr(cfgmod, "CONFIG_JSON", tmp_path / "config.json")
    # settings_screen imports save_config from config; patching the module attr is enough
    return tmp_path


def _app() -> CCUsageApp:
    # Empty engine: no transcripts needed to exercise navigation; snapshot still renders
    # (empty windows + flat heartbeat). Avoids depending on real ~/.claude data.
    # cache_path=None keeps it fully in-memory — never reads/writes the real parse cache.
    eng = Engine(Config(), cache_path=None)
    eng._scanned = True  # skip the disk scan; records stay []
    return CCUsageApp(eng)


def _app_with_records() -> CCUsageApp:
    """An app whose engine carries a few deterministic records.

    The date-range screen needs a record floor (earliest record day) and something to
    aggregate. We seed records at fixed *ages* relative to now (a 40-day-old one sets the
    floor well before any preset window, a few recent ones populate Last-7-days), so the
    flows are exercised without depending on real ~/.claude data or wall-clock dates.
    """
    eng = Engine(Config(), cache_path=None)
    now = time.time()
    day = 86400

    def rec(age_days: float, inp: int, cost: float, model: str = "claude-opus-4-8"):
        return UsageRecord(
            ts=now - age_days * day,
            model_raw=model,
            model_norm=model,
            known=True,
            input_tokens=inp,
            output_tokens=inp // 2,
            cache_read=inp,
            cache_creation=0,
            cost=cost,
        )

    eng.parser.records = [
        rec(40, 1000, 20.0),  # old -> sets the floor ~40 days back
        rec(3, 500, 9.0),  # inside Last 7 days
        rec(1, 200, 4.0, "claude-sonnet-4-6"),  # inside Last 7 days, 2nd model
        rec(0, 50, 0.5),  # today
    ]
    eng._scanned = True  # skip the disk scan; use the seeded records
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

            # Settings list order: refresh, window, cost, theme.
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


def test_settings_highlight_restored_after_pick_without_arrow(tmp_config):
    """A picker returns to a focused, visibly highlighted Settings row."""

    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from cc_usage.settings_screen import ChoiceScreen, SettingsScreen

            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, SettingsScreen)
            lv = app.screen.query_one("#settings-list", ListView)
            assert lv.has_focus
            assert app.screen.focused is lv
            assert lv.index is not None
            assert lv.highlighted_child is not None
            assert lv.highlighted_child in list(lv.children)
            assert lv.highlighted_child.has_class("-highlight")

    asyncio.run(scenario())

def test_startup_scan_runs_in_background_and_fills_panel(tmp_config, tmp_path, monkeypatch):
    """A not-yet-scanned engine must NOT block on_mount: the first scan runs in a worker
    thread (proven by capturing the thread it executes on), then the panel fills in,
    is_scanned flips, and a warm-start cache is written.

    (The other TUI tests seed `_scanned=True` to skip this path; this one exercises it.)
    We assert the scan ran OFF the main thread rather than trying to observe the in-flight
    'not yet scanned' state — for a one-line transcript the worker can finish before the
    first assertion, so that observation is racy; the thread identity is deterministic.
    """
    import threading

    import cc_usage.parser as P

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "r1",
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        + "\n"
    )

    eng = Engine(Config(), cache_path=tmp_path / "cache.pkl")

    # Wrap scan() to record which thread it actually runs on. If on_mount blocked
    # (scanned synchronously) this would be the main thread; the worker path must not.
    scan_ran = {}
    orig_scan = eng.scan

    def _recording_scan(*args, **kwargs):
        scan_ran["on_main_thread"] = threading.current_thread() is threading.main_thread()
        orig_scan(*args, **kwargs)
    monkeypatch.setattr(eng, "scan", _recording_scan)
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()  # let the background scan finish
            await pilot.pause()
            assert scan_ran.get("on_main_thread") is False  # scan ran OFF the UI thread
            assert eng.is_scanned is True
            assert len(eng.parser.records) == 1
            # The scan persisted a warm-start cache for next launch.
            assert (tmp_path / "cache.pkl").exists()

    asyncio.run(scenario())


def _multi_account_app() -> CCUsageApp:
    """An app whose engine reports two enabled Claude accounts + tagged records, so the
    account scope UI (the `a` key + scope line) and the by-account block are active."""
    from pathlib import Path

    from cc_usage.accounts import Root

    eng = Engine(Config(), cache_path=None)
    eng.roots = [
        Root("personal", Path("/x/.claude"), Path("/x/.claude/projects"), "auto"),
        Root("rdqcc", Path("/x/.claude-rdqcc"), Path("/x/.claude-rdqcc/projects"), "config"),
    ]
    now = time.time()

    def rec(account: str, inp: int, cost: float):
        return UsageRecord(
            ts=now - 100, model_raw="claude-opus-4-8", model_norm="claude-opus-4-8", known=True,
            input_tokens=inp, output_tokens=0, cache_read=0, cache_creation=0, cost=cost, account=account,
        )

    eng.parser.records = [rec("personal", 100, 2.0), rec("rdqcc", 300, 6.0)]
    eng._scanned = True
    eng._refresh_account_flags()
    return CCUsageApp(eng)


def test_account_scope_cycles_with_a_key(tmp_config):
    """`a` cycles all -> personal -> rdqcc -> all for a multi-account engine, and the
    scope line reflects it; a single-account app treats `a` as a no-op."""

    async def scenario():
        app = _multi_account_app()
        async with app.run_test() as pilot:
            from textual.widgets import Static

            assert app.engine.account_scope == "all"
            await pilot.press("a")
            assert app.engine.account_scope == "personal"
            scope_text = str(app.query_one("#scope", Static).renderable)
            assert "account" in scope_text and "personal" in scope_text
            await pilot.press("a")
            assert app.engine.account_scope == "rdqcc"
            await pilot.press("a")
            assert app.engine.account_scope == "all"

    asyncio.run(scenario())


def test_account_scope_key_is_noop_for_single_account(tmp_config):
    async def scenario():
        app = _app()  # single default account, no codex -> account UI inert
        async with app.run_test() as pilot:
            from textual.widgets import Static

            assert app.engine.account_scope == "all"
            await pilot.press("a")
            assert app.engine.account_scope == "all"  # no-op
            assert str(app.query_one("#scope", Static).renderable) == ""  # no scope line

    asyncio.run(scenario())


def test_settings_accounts_row_and_toggle(tmp_config, monkeypatch):
    """Multi-account Settings shows an Accounts row; opening it and pressing Enter on a
    root toggles its enabled flag, persisted to config.disabled_roots (R7)."""
    from pathlib import Path

    import cc_usage.settings_screen as ss
    from cc_usage.accounts import Root

    fake_roots = [
        Root("personal", Path("/x/.claude"), Path("/x/.claude/projects"), "auto"),
        Root("rdqcc", Path("/x/.claude-rdqcc"), Path("/x/.claude-rdqcc/projects"), "config"),
    ]
    monkeypatch.setattr(ss, "discover_claude_roots", lambda cfg: fake_roots)

    async def scenario():
        app = _multi_account_app()
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from cc_usage.settings_screen import AccountsScreen, SettingsScreen

            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            values = [item.value for item in app.screen.query_one("#settings-list", ListView).children]
            assert "accounts" in values  # the Accounts row is present

            # Navigate to the Accounts row (last) and open it.
            for _ in range(values.index("accounts")):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AccountsScreen)

            # Enter on the highlighted (first) root toggles it off, persisted to config.
            await pilot.press("enter")
            await pilot.pause()
            assert "/x/.claude" in app.config.disabled_roots

    asyncio.run(scenario())


def _two_real_roots(tmp_path):
    """Two on-disk Claude roots with one record each; returns (r1, r2) config dirs."""
    r1 = tmp_path / "personal-root"
    r2 = tmp_path / "rdqcc-root"

    def line(req: str, mid: str, inp: int) -> str:
        return (
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": req,
                    "timestamp": "2026-06-01T00:00:00.000Z",
                    "message": {
                        "id": mid,
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": inp, "output_tokens": 1},
                    },
                }
            )
            + "\n"
        )

    (r1 / "projects").mkdir(parents=True)
    (r2 / "projects").mkdir(parents=True)
    (r1 / "projects" / "s.jsonl").write_text(line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(line("b", "b", 200))
    return r1, r2


def _mock_two_root_discovery(tmp_path, monkeypatch):
    """Point engine + settings discovery at two real tmp roots whose enabled state
    follows config.disabled_roots (like the real discovery); keep everything else
    (legacy PROJECTS_DIR fallback, Codex dirs) hermetic. Returns (r1, r2)."""
    import cc_usage.engine as engine_module
    import cc_usage.parser as parser_module
    import cc_usage.settings_screen as ss
    from cc_usage.accounts import Root

    r1, r2 = _two_real_roots(tmp_path)

    def fake_discover(cfg):
        disabled = set(cfg.disabled_roots)
        return [
            Root("personal", r1, r1 / "projects", "auto", enabled=str(r1) not in disabled),
            Root("rdqcc", r2, r2 / "projects", "config", enabled=str(r2) not in disabled),
        ]

    monkeypatch.setattr(engine_module, "discover_claude_roots", fake_discover)
    monkeypatch.setattr(ss, "discover_claude_roots", fake_discover)
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", r1 / "projects")
    monkeypatch.setattr(engine_module, "CODEX_SESSIONS_DIR", tmp_path / "no-codex")
    monkeypatch.setattr(engine_module, "CODEX_ARCHIVED_SESSIONS_DIR", tmp_path / "no-codex2")
    monkeypatch.setattr(engine_module, "LIMITS_CACHE_JSON", tmp_path / "limits.json")
    return r1, r2


def test_settings_close_after_root_toggle_rescans(tmp_config, tmp_path, monkeypatch):
    """R7 wiring, end to end through the real keys: toggling a root in Settings →
    Accounts and closing Settings must run _on_settings_closed → reload_roots → a
    background rescan, after which the disabled root's records are gone."""
    _r1, r2 = _mock_two_root_discovery(tmp_path, monkeypatch)
    eng = Engine(Config(), cache_path=None)
    monkeypatch.setattr(eng, "refresh_limits", lambda: None)
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from cc_usage.settings_screen import AccountsScreen, SettingsScreen

            await app.workers.wait_for_complete()  # initial cold scan (both roots)
            await pilot.pause()
            assert {r.account for r in eng.parser.records} == {"personal", "rdqcc"}

            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            values = [
                item.value
                for item in app.screen.query_one("#settings-list", ListView).children
            ]
            for _ in range(values.index("accounts")):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AccountsScreen)

            await pilot.press("down")  # highlight rdqcc (row 1)
            await pilot.press("enter")  # toggle it off (persisted to config)
            await pilot.pause()
            assert str(r2) in app.config.disabled_roots

            await pilot.press("escape")  # Accounts -> Settings
            await pilot.pause()
            await pilot.press("escape")  # Settings closes -> _on_settings_closed
            await pilot.pause()
            for _ in range(600):  # let the rescan worker finish
                await pilot.pause()
                if eng.is_scanned and not app._scan_in_progress:
                    break

            assert eng.is_scanned is True
            assert {r.account for r in eng.parser.records} == {"personal"}  # rdqcc gone

    asyncio.run(scenario())


def test_root_toggle_during_scan_relaunches_once_and_keeps_cache_sane(
    tmp_config, tmp_path, monkeypatch
):
    """The toggle-during-scan race, at the app level: a root toggle landing while the
    background scan is mid-flight must cancel the stale worker, relaunch exactly one
    scan of the new root set, never persist an empty cache, and end with the panel
    showing data (not a stuck status line)."""
    import pickle
    import threading

    from cc_usage.parser import ScanCancelled

    _r1, r2 = _mock_two_root_discovery(tmp_path, monkeypatch)
    cache = tmp_path / "cache.pkl"
    eng = Engine(Config(), cache_path=cache)
    monkeypatch.setattr(eng, "refresh_limits", lambda: None)

    scans_completed = []
    orig_engine_scan = eng.scan

    def counting_scan(*args, **kwargs):
        orig_engine_scan(*args, **kwargs)
        scans_completed.append(1)

    monkeypatch.setattr(eng, "scan", counting_scan)

    # Hold the FIRST parser scan open until released (the in-flight cold scan).
    started = threading.Event()
    release = threading.Event()
    stale_parser = eng.parser
    orig_parser_scan = stale_parser.scan

    def blocking_scan(progress=None, cancelled=None):
        started.set()
        while not release.is_set():
            if cancelled is not None and cancelled():
                raise ScanCancelled("cancelled in test")
            time.sleep(0.005)
        return orig_parser_scan(progress=progress, cancelled=cancelled)

    monkeypatch.setattr(stale_parser, "scan", blocking_scan)
    app = CCUsageApp(eng)

    async def scenario():
        async with app.run_test() as pilot:
            from rich.table import Table
            from textual.widgets import Static

            for _ in range(400):
                if started.is_set():
                    break
                await pilot.pause()
            assert started.is_set()  # the worker is inside the stale parser's scan

            # The user toggles rdqcc off and closes Settings while the scan runs.
            app.config.disabled_roots = [str(r2)]
            app._on_settings_closed()
            assert app._rescan_pending is True  # queued behind the in-flight worker
            release.set()  # let the stale worker observe the cancellation

            for _ in range(600):  # old worker dies -> relaunch -> rescan completes
                await pilot.pause()
                if eng.is_scanned and not app._scan_in_progress and not app._rescan_pending:
                    break

            assert eng.is_scanned is True
            assert {r.account for r in eng.parser.records} == {"personal"}
            assert len(scans_completed) == 1  # exactly one scan completed: the relaunch
            # The panel ended non-empty: the spend widget holds a data table again,
            # not a scanning/cancelled status line.
            assert isinstance(app.query_one("#spend", Static).renderable, Table)
            # No empty cache was persisted; the on-disk cache holds the new root's data.
            assert cache.exists()
            with open(cache, "rb") as fh:
                data = pickle.load(fh)
            assert data["records"], "an EMPTY record list was persisted"
            assert data["files"], "an EMPTY file map was persisted"

    asyncio.run(scenario())


def test_quit_is_clean(tmp_config):
    async def scenario():
        app = _app()
        async with app.run_test() as pilot:
            await pilot.press("q")
        # run_test() exits the context without raising -> clean quit + restore
        assert app.return_code in (0, None)

    asyncio.run(scenario())


# ── T7: date-range analysis screen ───────────────────────────────────────────────
def test_date_range_opens_and_highlight_visible_immediately(tmp_config):
    """`d` opens RangeScreen; the controls list is focused AND visibly highlighted with
    NO pre-press (the highlight bug we must not regress); ↑/↓ move the highlight."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from cc_usage.range_screen import RangeScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)

            lv = app.screen.query_one("#range-controls", ListView)
            # Highlight visible immediately, before any arrow press:
            assert lv.has_focus
            assert lv.index == 0
            assert lv.highlighted_child is not None
            assert lv.highlighted_child.has_class("-highlight")

            # ↑/↓ move the control highlight (Preset -> Start -> End -> wrap is not asserted,
            # just that down advances and the new row is highlighted).
            await pilot.press("down")
            assert lv.index == 1
            assert lv.highlighted_child.has_class("-highlight")
            await pilot.press("down")
            assert lv.index == 2
            await pilot.press("up")
            assert lv.index == 1

    asyncio.run(scenario())


def test_date_range_preset_updates_results(tmp_config):
    """Enter on Preset opens the ChoiceScreen; choosing a different preset updates the
    screen's range. Then Esc returns to the main panel and the heartbeat arrows work."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from cc_usage.range_screen import RangeScreen
            from cc_usage.settings_screen import ChoiceScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            assert app.screen.preset_key == "last7"  # default range on open
            span_before = (app.screen.end_date - app.screen.start_date).days
            assert span_before == 6  # Last 7 days inclusive

            # Preset is control row 0; Enter opens the picker.
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            # PRESETS order: last7, last30, thismonth, lastmonth, all, custom.
            await pilot.press("down")  # last7 -> last30
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            assert app.screen.preset_key == "last30"
            assert (app.screen.end_date - app.screen.start_date).days == 29

            # Back to the main panel, and the heartbeat arrows work again (were gated).
            base_window = app.engine.hb_window
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RangeScreen)
            await pilot.press("right")  # heartbeat window must advance again
            assert app.engine.hb_window != base_window

    asyncio.run(scenario())


def test_date_range_stepper_changes_date_and_keeps_invariant(tmp_config):
    """Enter on Start opens the DateStepperScreen; ↑/↓ (±day), ←/→ (±month) change the
    date; Enter applies and the results update; start <= end always holds."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from cc_usage.range_screen import DateStepperScreen, RangeScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            start_before = app.screen.start_date

            # Move to the Start control (row 1) and open the stepper.
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DateStepperScreen)

            # ↓ = -1 day, ← = -1 month. Step the start date earlier, then confirm.
            await pilot.press("down")  # -1 day
            await pilot.press("left")  # -1 month
            await pilot.press("enter")  # confirm
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            # The start date actually moved earlier and the invariant holds.
            assert app.screen.start_date < start_before
            assert app.screen.start_date <= app.screen.end_date
            assert app.screen.preset_key == "custom"  # a manual edit marks it custom

            # Now push the END below start to prove auto-correction. Move to End (row 2).
            await pilot.press("down")  # row 1 (start) -> row 2 (end)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, DateStepperScreen)
            # Hammer ↓ many times to drive end far below start; the stepper clamps to the
            # floor, and on apply the screen pulls start down to keep start <= end.
            for _ in range(400):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            assert app.screen.start_date <= app.screen.end_date  # invariant preserved

    asyncio.run(scenario())


def test_date_range_esc_returns_and_panel_intact(tmp_config):
    """Esc from RangeScreen returns to the main panel; the panel's widgets are still
    present and the heartbeat arrows fire on the base screen again."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from textual.widgets import Static

            from cc_usage.range_screen import RangeScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)

            # `m` toggles the chart metric without leaving the screen.
            assert app.screen.chart_metric == "cost"
            await pilot.press("m")
            assert app.screen.chart_metric == "tokens"

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RangeScreen)

            # Main panel widgets intact + heartbeat still drivable by arrows.
            hb = app.query_one("#hb", Static)
            assert hb.renderable is not None
            metric_before = app.engine.hb_metric
            await pilot.press("down")  # toggles heartbeat metric on the base panel
            assert app.engine.hb_metric != metric_before

    asyncio.run(scenario())


def test_date_range_arrows_gated_while_open(tmp_config):
    """While RangeScreen is on top, the panel's priority heartbeat arrows must NOT fire —
    ↑/↓ drive the controls list, not the heartbeat metric (the modal-gating contract)."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from cc_usage.range_screen import RangeScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            metric_before = app.engine.hb_metric
            await pilot.press("down")  # should move the list, not flip the metric
            assert app.engine.hb_metric == metric_before  # ↓ did not leak to the panel

    asyncio.run(scenario())


def test_date_range_base_keys_do_not_leak_into_screen(tmp_config):
    """F1/F2: while RangeScreen is open, the base-app keys that PUSH a screen (`d` ->
    date_range, `s` -> settings) must NOT fire — they would stack a second RangeScreen /
    a Settings screen on top. `t` (hb_metric) and `r` (refresh_now) must not fire either.
    The screen stack must stay exactly ['Screen', 'RangeScreen'] after pressing them, and
    Esc/q must still return to the panel."""

    async def scenario():
        app = _app_with_records()
        async with app.run_test() as pilot:
            from cc_usage.range_screen import RangeScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            base_stack = [type(s).__name__ for s in app.screen_stack]
            assert base_stack == ["Screen", "RangeScreen"]

            # `d` must NOT push a SECOND RangeScreen (F1).
            await pilot.press("d")
            await pilot.pause()
            assert [type(s).__name__ for s in app.screen_stack] == base_stack
            assert isinstance(app.screen, RangeScreen)

            # `s` must NOT open Settings on top of RangeScreen (F2).
            await pilot.press("s")
            await pilot.pause()
            assert [type(s).__name__ for s in app.screen_stack] == base_stack
            assert isinstance(app.screen, RangeScreen)

            # `t` (hb_metric) and `r` (refresh_now) must not change the stack either.
            metric_before = app.engine.hb_metric
            await pilot.press("t")
            await pilot.press("r")
            await pilot.pause()
            assert [type(s).__name__ for s in app.screen_stack] == base_stack
            assert app.engine.hb_metric == metric_before  # `t` didn't leak to the panel

            # Esc still returns to the panel (one pop reaches the base, not a pile of them).
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RangeScreen)
            assert [type(s).__name__ for s in app.screen_stack] == ["Screen"]

            # The heartbeat arrows work again on the panel.
            window_before = app.engine.hb_window
            await pilot.press("right")
            assert app.engine.hb_window != window_before

    asyncio.run(scenario())


def _app_with_recent_records_only() -> CCUsageApp:
    """An app whose ENTIRE data history is only ~4 days old (earliest record 4 days back).

    This makes the earliest-record floor MORE RECENT than the start of "Last 7/30 days",
    so the buggy floor-clamp would visibly TRUNCATE those presets. Used to prove F3."""
    eng = Engine(Config(), cache_path=None)
    now = time.time()
    day = 86400

    def rec(age_days: float, inp: int, cost: float):
        return UsageRecord(
            ts=now - age_days * day,
            model_raw="claude-opus-4-8",
            model_norm="claude-opus-4-8",
            known=True,
            input_tokens=inp,
            output_tokens=inp // 2,
            cache_read=inp,
            cache_creation=0,
            cost=cost,
        )

    eng.parser.records = [rec(4, 1000, 20.0), rec(1, 200, 4.0), rec(0, 50, 0.5)]
    eng._scanned = True
    return CCUsageApp(eng)


def test_date_range_preset_keeps_literal_calendar_span(tmp_config):
    """F3: a preset reflects its LITERAL calendar span and is NOT floor-clamped to the
    earliest record. Here the whole history is only 4 days old, so the floor is today-4 —
    MORE recent than the "Last 30 days" start (today-29) and the "Last 7 days" start
    (today-6). The presets must STILL span the full 30 / 7 calendar days (the leading
    pre-data days are simply zero-filled), NOT get snapped up to the 4-day-old floor."""

    async def scenario():
        app = _app_with_recent_records_only()
        async with app.run_test() as pilot:
            from cc_usage.range_screen import RangeScreen
            from cc_usage.settings_screen import ChoiceScreen

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            screen = app.screen
            # Default Last 7 days: literal 7-day span even though data starts 4 days ago.
            assert (screen.end_date - screen.start_date).days == 6
            assert screen._compute_range().n_days == 7

            # Pick "Last 30 days" — its start (today-29) is well before the today-4 floor,
            # but the span stays a literal 30 days (the floor must NOT snap it up).
            await pilot.press("enter")  # open preset picker
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("down")  # last7 -> last30
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RangeScreen)
            assert app.screen.preset_key == "last30"
            assert (app.screen.end_date - app.screen.start_date).days == 29
            assert app.screen.end_date == datetime.date.today()
            # The by-day chart/table reflect the full 30-day span (zero-filled days).
            assert app.screen._compute_range().n_days == 30

    asyncio.run(scenario())
