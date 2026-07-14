"""Normalize and render provider usage-limit buckets.

Current versions receive normalized captures from direct Claude and Codex fetchers.
The legacy JSON loader remains for compatibility. This module never raises.
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

def label_for_minutes(minutes: object, fallback: str) -> str:
    """Human label for Codex's duration-based primary/secondary buckets."""
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return label_for(fallback)
    value = int(minutes)
    if value % 10_080 == 0:
        weeks = value // 10_080
        return "WEEKLY" if weeks == 1 else f"{weeks}-WEEK"
    if value % 1_440 == 0:
        return f"{value // 1_440}-DAY"
    if value % 60 == 0:
        return f"{value // 60}-HOUR"
    return f"{value}-MIN"



def load_ratelimits(path: Path = RATELIMITS_JSON) -> dict | None:
    """Parsed capture dict, or None if absent/unreadable. Never raises."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def get_buckets(data: dict | None, provider: str | None = None) -> list[Bucket]:
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
        label = val.get("label")
        if not isinstance(label, str) or not label:
            label = label_for_minutes(val.get("window_minutes"), key)
        if provider:
            label = f"{provider.upper()} {label}"
        found.append(
            Bucket(key=key, label=label, used_percentage=float(up), resets_at=float(ra))
        )

    def sort_key(b: Bucket) -> tuple[int, str]:
        return (_ORDER.index(b.key) if b.key in _ORDER else len(_ORDER), b.key)

    return sorted(found, key=sort_key)


def provider_buckets(claude: dict | None, codex: dict | None) -> list[Bucket]:
    """Return both providers' limits; never collapse one into the other."""
    return get_buckets(claude, "Claude") + get_buckets(codex, "Codex")


def account_buckets(
    captures: dict[str, dict], claude_labels: list[str], *, multi: bool
) -> list[Bucket]:
    """Per-account limit buckets (T11 R5), Claude accounts first then Codex.

    `captures` is keyed `claude:<label>` per account plus `codex`. With a single
    Claude account the prefix stays `CLAUDE …`, byte-identical to before
    multi-account; with several, each account's buckets are prefixed with the
    account label (`PERSONAL 5-HOUR`, `RDQCC 5-HOUR`) so they never blur together.
    Codex buckets always keep the `CODEX` prefix. One account's missing capture
    simply contributes no rows — it never blocks the others.
    """
    out: list[Bucket] = []
    for label in claude_labels:
        prefix = label if multi else "Claude"
        out += get_buckets(captures.get(f"claude:{label}"), prefix)
    out += get_buckets(captures.get("codex"), "Codex")
    return out
