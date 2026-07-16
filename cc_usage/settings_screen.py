"""In-app Settings (T3 R3) — replaces v1's `--config`; 100% keyboard-driven.

A Textual screen reached from the main TUI by pressing `s` (or Enter on the Settings
hint). Arrow keys move the selection, Enter activates, Esc backs out. Every setting is
chosen from a fixed list of choices — never raw text entry — mirroring the v1 rule.

Covers refresh interval, default table window, show-cost, and theme. Provider limits are
fetched automatically and need no setting. Changes persist and apply live.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, ListItem, ListView, Static

from .accounts import CLAUDE_PROVIDER, CODEX_PROVIDER, discover_claude_roots, discover_codex_roots
from .config import (
    REFRESH_CHOICES,
    THEME_CHOICES,
    WINDOW_CHOICES,
    Config,
    save_config,
)

_WINDOW_LABEL = {
    "all": "all-time",
    "1h": "last 1h",
    "5h": "last 5h",
    "24h": "last 24h",
    "7d": "last 7d",
}


def _summary(cfg: Config) -> list[tuple[str, str, str]]:
    """(row id, left label, current-value) for the top-level settings list."""
    return [
        ("refresh", "Refresh interval", f"{cfg.refresh_interval}s"),
        ("window", "Default table window", _WINDOW_LABEL.get(cfg.default_window, cfg.default_window)),
        ("cost", "Show cost column", "on" if cfg.show_cost else "off"),
        ("theme", "Theme", cfg.theme),
    ]


class _ChoiceRow(ListItem):
    """One selectable row that carries the value it represents."""

    def __init__(self, value, text: str) -> None:
        super().__init__(Label(text))
        self.value = value


class ChoiceScreen(ModalScreen):
    """A generic arrow-key picker: choose one value from a list, Enter confirms."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Back", show=True),
        Binding("q", "dismiss_none", "Back", show=False),
    ]

    def __init__(self, title: str, choices: list[tuple[object, str]], current=None) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._current = current

    def compose(self) -> ComposeResult:
        items = [
            _ChoiceRow(val, f"{'●' if val == self._current else ' '} {text}")
            for val, text in self._choices
        ]
        # Start the highlight on the current value so Enter without moving keeps it;
        # default to the first row otherwise (never an empty/None highlight).
        start = 0
        for i, (val, _t) in enumerate(self._choices):
            if val == self._current:
                start = i
                break
        lv = ListView(*items, id="choices", initial_index=start)
        with Center():
            with Vertical(id="choice-box"):
                yield Static(self._title, id="choice-title")
                yield lv
                yield Static("● current · ↑/↓ move · ↵ select · Esc back", id="choice-hint")

    def on_mount(self) -> None:
        self.query_one("#choices", ListView).focus()

    @on(ListView.Selected, "#choices")
    def _picked(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.value)  # type: ignore[attr-defined]

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def _discovered_roots(config):
    """All discovered account roots (Claude then Codex, T12), each tagged with its
    provider for display. Codex reuses the Claude labels for reservation so labels
    stay unique across providers."""
    claude = discover_claude_roots(config)
    codex = discover_codex_roots(config, claude_roots=claude)
    return [(CLAUDE_PROVIDER, r) for r in claude] + [(CODEX_PROVIDER, r) for r in codex]


class AccountsScreen(ModalScreen):
    """Read-only list of discovered account roots with an enable toggle (T11 R7, T12).

    Lists every Claude *and* Codex root (label, provider, source, path). Arrow keys
    move, Enter toggles the highlighted root's `enabled` flag (persisted to
    `config.json`'s `disabled_roots`, keyed by path so it works for either provider),
    Esc backs out. Adding a *new* root is a manual `config.json` edit — surfaced in
    the hint. Toggling here changes what the next rescan reads; the parent Settings
    screen's close handler triggers that rescan.
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "Back", show=True),
        Binding("q", "dismiss_none", "Back", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="choice-box"):
                yield Static("Accounts", id="choice-title")
                yield ListView(id="accounts-list")
                yield Static(
                    "● enabled · ↑/↓ move · ↵ toggle · Esc back  (add a root: edit config.json)",
                    id="choice-hint",
                )

    async def on_mount(self) -> None:
        await self._refresh_list()

    async def _refresh_list(self, keep_index: int | None = None) -> None:
        lv = self.query_one("#accounts-list", ListView)
        idx = lv.index if keep_index is None else keep_index
        if idx is None:
            idx = 0
        await lv.clear()
        rows = []
        for provider, root in _discovered_roots(self.config):
            mark = "●" if root.enabled else "○"
            rows.append(
                _ChoiceRow(
                    str(root.path),
                    f"{mark} {root.label:<12}  {provider:<6}  {root.source:<6}  {root.path}",
                )
            )
        await lv.extend(rows)
        lv.index = max(0, min(idx, len(lv) - 1))
        self._focus_list()

    def _focus_list(self) -> None:
        lv = self.query_one("#accounts-list", ListView)
        lv.focus()
        if len(lv) == 0:
            return
        idx = 0 if lv.index is None else max(0, min(lv.index, len(lv) - 1))
        lv.index = idx
        child = lv.highlighted_child
        if child is not None:
            child.highlighted = True

    @on(ListView.Selected, "#accounts-list")
    def _toggle(self, event: ListView.Selected) -> None:
        path = event.item.value  # type: ignore[attr-defined]
        disabled = [p for p in self.config.disabled_roots if isinstance(p, str)]
        if path in disabled:
            disabled.remove(path)
        else:
            disabled.append(path)
        self.config.disabled_roots = disabled
        save_config(self.config)
        keep = self.query_one("#accounts-list", ListView).index
        self.run_worker(self._refresh_list(keep), exclusive=True)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen):
    """Top-level Settings list. Selecting a row opens the matching picker."""

    BINDINGS = [
        Binding("escape", "close", "Back to panel", show=True),
        Binding("q", "close", "Back", show=False),
    ]

    def __init__(self, config: Config, engine=None) -> None:
        super().__init__()
        self.config = config
        # The engine (optional) exposes discovered roots so the Accounts row appears
        # only for multi-root setups — single-account Settings is unchanged.
        self.engine = engine

    def _extra_rows(self) -> list[tuple[str, str, str]]:
        """The Accounts management row, shown only when either provider has more than
        one root (so a plain single `~/.claude` + `~/.codex` machine never sees it)."""
        if self.engine is None:
            return []
        claude = getattr(self.engine, "roots", [])
        codex = getattr(self.engine, "codex_roots", [])
        if len(claude) <= 1 and len(codex) <= 1:
            return []
        roots = [root for _provider, root in _discovered_roots(self.config)]
        enabled = sum(1 for r in roots if r.enabled)
        return [("accounts", "Accounts", f"{enabled}/{len(roots)} enabled")]

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="settings-box"):
                yield Static("Settings", id="settings-title")
                yield ListView(id="settings-list")
                yield Static(
                    "↑/↓ move · ↵ change · Esc back to panel  (keyboard-only)",
                    id="settings-hint",
                )
        yield Footer()

    async def on_mount(self) -> None:
        # _refresh_list ends by focusing the list and asserting its highlight.
        await self._refresh_list()

    def on_screen_resume(self) -> None:
        # Fired whenever a value picker pops. The callback already rebuilt the list,
        # but a ListView only paints its highlight
        # cursor while focused — and the screen-pop does not return focus to our list.
        # So re-focus it here, and re-assert the highlight, so the cursor is visible
        # immediately with no arrow press. call_after_refresh defends against the
        # screen-pop's own focus restoration landing after this handler.
        self.call_after_refresh(self._focus_list)

    def _focus_list(self) -> None:
        """Focus the settings list and force its highlight cursor to (re)paint.

        The list is rebuilt via the *awaited* _refresh_list (children fully mounted
        before the index is set), so the highlighted ListItem already carries the
        `-highlight` class. We still re-toggle it defensively: the class only shows
        the cursor while the ListView is focused, which we ensure first.
        """
        lv = self.query_one("#settings-list", ListView)
        lv.focus()
        if len(lv) == 0:
            return
        idx = 0 if lv.index is None else max(0, min(lv.index, len(lv) - 1))
        lv.index = idx
        child = lv.highlighted_child
        if child is not None:
            child.highlighted = True

    async def _refresh_list(self, keep_index: int | None = None) -> None:
        lv = self.query_one("#settings-list", ListView)
        idx = lv.index if keep_index is None else keep_index
        if idx is None:
            idx = 0  # always start with a visible highlight on the first row
        # AWAIT the clear + mounts so the children are fully settled before we set
        # the index. Otherwise watch_index toggles `-highlight` onto items that are
        # still mounting and the class is lost — the highlight cursor then stays
        # invisible until an arrow key re-runs the highlight (the reported bug).
        await lv.clear()
        rows = [
            _ChoiceRow(row_id, f"{left:<26}· {value}")
            for row_id, left, value in [*_summary(self.config), *self._extra_rows()]
        ]
        await lv.extend(rows)
        lv.index = max(0, min(idx, len(lv) - 1))
        # Children are now fully mounted and the index is set, so the highlight has
        # stuck. Make it visible: focus the list and (re)assert the highlight class.
        self._focus_list()

    def _refresh_list_soon(self, keep_index: int | None = None) -> None:
        """Schedule the async rebuild from a sync (pop-callback) context.

        Used by value-picker callbacks, which are synchronous.
        The rebuild ends by focusing the list and re-asserting its highlight, so the
        Settings menu comes back visibly highlighted with no arrow press needed.
        """
        self.run_worker(self._refresh_list(keep_index), exclusive=True)

    # ── interactions ─────────────────────────────────────────────────────────
    @on(ListView.Selected, "#settings-list")
    def _row_selected(self, event: ListView.Selected) -> None:
        row_id = event.item.value  # type: ignore[attr-defined]
        keep = self.query_one("#settings-list", ListView).index
        if row_id == "refresh":
            self._pick(
                "Refresh interval",
                [(s, f"{s}s") for s in REFRESH_CHOICES],
                self.config.refresh_interval,
                self._set_refresh,
                keep,
            )
        elif row_id == "window":
            self._pick(
                "Default table window",
                [(w, _WINDOW_LABEL[w]) for w in WINDOW_CHOICES],
                self.config.default_window,
                self._set_window,
                keep,
            )
        elif row_id == "cost":
            self._pick(
                "Show cost column",
                [(True, "on"), (False, "off")],
                self.config.show_cost,
                self._set_cost,
                keep,
            )
        elif row_id == "theme":
            self._pick(
                "Theme",
                [(t, t) for t in THEME_CHOICES],
                self.config.theme,
                self._set_theme,
                keep,
            )
        elif row_id == "accounts":
            # Toggling a root persists to config inside AccountsScreen; on return just
            # rebuild this list so the "N/M enabled" summary reflects the change. The
            # rescan happens when the Settings screen itself closes (app handler).
            self.app.push_screen(
                AccountsScreen(self.config), lambda _=None: self._refresh_list_soon(keep)
            )

    def _pick(self, title, choices, current, setter, keep) -> None:
        def done(value) -> None:
            if value is not None:
                setter(value)
                save_config(self.config)
                self._apply_live()
            self._refresh_list_soon(keep)

        self.app.push_screen(ChoiceScreen(title, choices, current), done)


    # ── setters ──────────────────────────────────────────────────────────────
    def _set_refresh(self, v) -> None:
        self.config.refresh_interval = v

    def _set_window(self, v) -> None:
        self.config.default_window = v

    def _set_cost(self, v) -> None:
        self.config.show_cost = v

    def _set_theme(self, v) -> None:
        self.config.theme = v

    def _apply_live(self) -> None:
        # Tell the running app to re-render with the new config + refresh cadence.
        apply = getattr(self.app, "apply_config", None)
        if callable(apply):
            apply()

    def action_close(self) -> None:
        save_config(self.config)
        self.dismiss(None)
