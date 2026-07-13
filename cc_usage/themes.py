"""A few Rich style palettes (T0 §7). Each maps semantic roles to style strings.

`bar_style(theme, pct)` picks the gauge colour by the shared <50 / <80 / >=80
usage thresholds.
"""

from __future__ import annotations

# Anthropic-inspired warm palette for the default dark theme.
_DARK = {
    "border": "#d7875f",
    "title": "bold #ffd7af",
    "subtitle": "#8a8a8a",
    "label": "bold #d7af87",
    "header": "bold #d7af87",
    "value": "#ffd7af",
    "model": "#d7af87",
    "total": "bold #ffd7af",
    "dim": "#8a8a8a",
    "warn": "bold #d75f5f",
    "good": "#87af5f",
    "bar_low": "#87af5f",
    "bar_mid": "#d7af5f",
    "bar_high": "#d75f5f",
    "bar_empty": "#585858",
}

_LIGHT = {
    "border": "#af5f00",
    "title": "bold #5f3700",
    "subtitle": "#767676",
    "label": "bold #5f3700",
    "header": "bold #5f3700",
    "value": "#303030",
    "model": "#5f5f00",
    "total": "bold #303030",
    "dim": "#767676",
    "warn": "bold #d70000",
    "good": "#008700",
    "bar_low": "#008700",
    "bar_mid": "#af8700",
    "bar_high": "#d70000",
    "bar_empty": "#bcbcbc",
}

_HIGH_CONTRAST = {
    "border": "bold bright_white",
    "title": "bold bright_white",
    "subtitle": "bright_white",
    "label": "bold bright_white",
    "header": "bold bright_white",
    "value": "bright_white",
    "model": "bright_white",
    "total": "bold bright_yellow",
    "dim": "grey70",
    "warn": "bold bright_red",
    "good": "bright_green",
    "bar_low": "bright_green",
    "bar_mid": "bright_yellow",
    "bar_high": "bright_red",
    "bar_empty": "grey42",
}

_THEMES = {"dark": _DARK, "light": _LIGHT, "high-contrast": _HIGH_CONTRAST}


def get_theme(name: str) -> dict[str, str]:
    return _THEMES.get(name, _DARK)


def bar_style(theme: dict[str, str], pct: float) -> str:
    if pct < 50:
        return theme["bar_low"]
    if pct < 80:
        return theme["bar_mid"]
    return theme["bar_high"]
