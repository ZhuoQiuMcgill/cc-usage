"""App config (T0 §7), persisted to ~/.config/cc-usage/config.json.

Every value is validated against an allowed set on load; anything unexpected
falls back to the default (never a crash, Rulebook rule 4).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .paths import CONFIG_JSON, ensure_dirs

REFRESH_CHOICES = [2, 5, 10, 30]
# T3 R1: 7d is selectable as the default table window, alongside all-time.
WINDOW_CHOICES = ["all", "1h", "5h", "24h", "7d"]
THEME_CHOICES = ["dark", "light", "high-contrast"]


@dataclass
class Config:
    refresh_interval: int = 5
    default_window: str = "all"
    show_cost: bool = True
    theme: str = "dark"
    # T11/T12 multi-account. `account_scope` is the last-selected panel scope ("all"
    # or an account label; validated against live accounts at runtime, not here).
    # `claude_roots` / `codex_roots` are extra transcript roots the user declared by
    # hand (each a list of {"path","label","enabled"} objects); `disabled_roots` are
    # root paths (either provider) toggled off in the settings screen.
    account_scope: str = "all"
    claude_roots: list = field(default_factory=list)
    codex_roots: list = field(default_factory=list)
    disabled_roots: list = field(default_factory=list)


def _sanitize_roots(value: object) -> list:
    """Keep only well-formed {"path": str, ...} root objects (never crash)."""
    if not isinstance(value, list):
        return []
    out: list = []
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]:
            out.append(entry)
    return out


def _validate(cfg: Config) -> Config:
    if cfg.refresh_interval not in REFRESH_CHOICES:
        cfg.refresh_interval = 5
    if cfg.default_window not in WINDOW_CHOICES:
        cfg.default_window = "all"
    if not isinstance(cfg.show_cost, bool):
        cfg.show_cost = True
    if cfg.theme not in THEME_CHOICES:
        cfg.theme = "dark"
    if not isinstance(cfg.account_scope, str) or not cfg.account_scope:
        cfg.account_scope = "all"
    cfg.claude_roots = _sanitize_roots(cfg.claude_roots)
    cfg.codex_roots = _sanitize_roots(cfg.codex_roots)
    cfg.disabled_roots = [p for p in cfg.disabled_roots if isinstance(p, str)] if isinstance(
        cfg.disabled_roots, list
    ) else []
    return cfg


def load_config() -> Config:
    try:
        raw = json.loads(CONFIG_JSON.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return Config()
    if not isinstance(raw, dict):
        return Config()
    cfg = Config()
    for key in (
        "refresh_interval",
        "default_window",
        "show_cost",
        "theme",
        "account_scope",
        "claude_roots",
        "codex_roots",
        "disabled_roots",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])
    return _validate(cfg)


def save_config(cfg: Config) -> None:
    ensure_dirs()
    _validate(cfg)
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n", "utf-8")
    tmp.replace(CONFIG_JSON)
