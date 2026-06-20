"""Human-friendly formatting: token counts (K/M/B), money ($), durations, names."""

from __future__ import annotations

import re

_MODEL_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?$")


def pretty_model_name(model_norm: str) -> str:
    """'claude-opus-4-8' -> 'Opus 4.8', 'claude-fable-5' -> 'Fable 5'."""
    m = _MODEL_RE.match(model_norm or "")
    if not m:
        return model_norm or "(unknown)"
    family = m.group(1).capitalize()
    if m.group(3):
        return f"{family} {m.group(2)}.{m.group(3)}"
    return f"{family} {m.group(2)}"


def human_tokens(n: int | float) -> str:
    """1234 -> '1K', 1_100_000 -> '1.1M', 12_300_000 -> '12.3M'."""
    try:
        n = int(round(n))
    except (TypeError, ValueError):
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.0f}K"
    return f"{sign}{n}"


def human_money(x: float) -> str:
    """58.05 -> '$58.05'. Always two decimals."""
    try:
        return f"${x:,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def human_duration(seconds: float) -> str:
    """Compact countdown: 7860 -> '2h11m', 356400 -> '4d03h', 65 -> '1m05s'.

    Negative / zero -> 'now' (the reset moment has passed between refreshes).
    """
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s <= 0:
        return "now"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d > 0:
        return f"{d}d{h:02d}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"
