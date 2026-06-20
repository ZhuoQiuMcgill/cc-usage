"""Read the captured 5h/7d limits (T0 §4B) from ~/.config/cc-usage/ratelimits.json.

The file is written by the reversible statusline wrapper (statusline.py). We render
*whatever* buckets appear (Guardrail 3 — don't hardcode two) and never assert weekly
semantics (Guardrail 2). If the file is missing or has no usable buckets, callers show
the n/a line (Guardrail 4 / T0 §10.6) — this module never raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .paths import RATELIMITS_JSON

# Friendly labels for the buckets we know; anything else gets a derived label.
_LABELS = {"five_hour": "5-HOUR", "seven_day": "WEEKLY"}
# Preferred left-to-right / top-to-bottom order; unknown buckets follow, sorted.
_ORDER = ["five_hour", "seven_day"]


@dataclass
class Bucket:
    key: str
    label: str
    used_percentage: float
    resets_at: float  # epoch seconds


def label_for(key: str) -> str:
    return _LABELS.get(key) or key.replace("_", " ").upper()


def load_ratelimits(path: Path = RATELIMITS_JSON) -> dict | None:
    """Parsed capture dict, or None if absent/unreadable. Never raises."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def get_buckets(data: dict | None) -> list[Bucket]:
    """Extract every well-formed bucket under .rate_limits, ordered deterministically."""
    if not isinstance(data, dict):
        return []
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        return []
    found: list[Bucket] = []
    for key, val in rl.items():
        if not isinstance(val, dict):
            continue
        up = val.get("used_percentage")
        ra = val.get("resets_at")
        if not isinstance(up, (int, float)) or not isinstance(ra, (int, float)):
            continue
        found.append(
            Bucket(key=key, label=label_for(key), used_percentage=float(up), resets_at=float(ra))
        )

    def sort_key(b: Bucket) -> tuple[int, str]:
        return (_ORDER.index(b.key) if b.key in _ORDER else len(_ORDER), b.key)

    return sorted(found, key=sort_key)


def captured_at(data: dict | None) -> float | None:
    if isinstance(data, dict):
        ts = data.get("captured_at")
        if isinstance(ts, (int, float)):
            return float(ts)
    return None
