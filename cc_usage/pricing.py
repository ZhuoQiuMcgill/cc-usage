"""Pricing as editable data (Rulebook rule 7), never magic constants.

Rates are USD per 1,000,000 tokens. The bundled defaults are copied to
~/.config/cc-usage/pricing.json on first run; the user may edit that file.
A malformed pricing.json degrades to the bundled defaults with a warning,
never a crash (Rulebook rule 4).
"""

from __future__ import annotations

import json
from importlib import resources

from .paths import PRICING_JSON, ensure_dirs

# Fallback if the bundled data file can't be read for some reason.
_HARDCODED_FALLBACK: dict[str, dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "gpt-5.6-sol": {
        "input": 5.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
        "output": 30.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gpt-5.6-terra": {
        "input": 2.5,
        "cache_read": 0.25,
        "cache_write": 3.125,
        "output": 15.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gpt-5.6-luna": {
        "input": 1.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
        "output": 6.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gpt-5.5": {
        "input": 5.0,
        "cache_read": 0.5,
        "output": 30.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gpt-5.4": {
        "input": 2.5,
        "cache_read": 0.25,
        "output": 15.0,
        "long_context_threshold": 272000.0,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gpt-5.4-mini": {"input": 0.75, "cache_read": 0.075, "output": 4.5},
}

_OPTIONAL_RATE_FIELDS = (
    "cache_read",
    "cache_write",
    "long_context_threshold",
    "long_context_input_multiplier",
    "long_context_output_multiplier",
)


def _bundled_defaults() -> dict[str, dict[str, float]]:
    try:
        raw = (resources.files("cc_usage") / "data" / "pricing.json").read_text("utf-8")
        models = json.loads(raw).get("models", {})
        if isinstance(models, dict) and models:
            return {k: v for k, v in models.items()}
    except Exception:
        pass
    return dict(_HARDCODED_FALLBACK)


def _coerce(models: object) -> dict[str, dict[str, float]]:
    """Keep well-formed rate rows while preserving supported optional fields."""
    out: dict[str, dict[str, float]] = {}
    if not isinstance(models, dict):
        return out
    for mid, row in models.items():
        if not isinstance(row, dict):
            continue
        try:
            parsed = {"input": float(row["input"]), "output": float(row["output"])}
        except (KeyError, TypeError, ValueError):
            continue
        for field in _OPTIONAL_RATE_FIELDS:
            if field not in row:
                continue
            try:
                parsed[field] = float(row[field])
            except (TypeError, ValueError):
                continue
        out[str(mid)] = parsed
    return out


def load_pricing() -> tuple[dict[str, dict[str, float]], list[str]]:
    """Return (pricing, warnings).

    Creates the user pricing.json from the bundled defaults if missing. If the
    user's file is malformed/empty, fall back to defaults and report a warning.
    """
    warnings: list[str] = []
    defaults = _bundled_defaults()

    ensure_dirs()
    if not PRICING_JSON.exists():
        try:
            PRICING_JSON.write_text(
                json.dumps(
                    {
                        "_comment": "USD per 1,000,000 tokens. Edit freely. "
                        "Unknown models keep their tokens and are shown as unpriced.",
                        "models": defaults,
                    },
                    indent=2,
                )
                + "\n",
                "utf-8",
            )
        except OSError as e:
            warnings.append(f"could not write {PRICING_JSON}: {e}")
        return defaults, warnings

    try:
        data = json.loads(PRICING_JSON.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        warnings.append(f"pricing.json unreadable ({e}); using bundled defaults")
        return defaults, warnings

    user = _coerce(data.get("models") if isinstance(data, dict) else None)
    if not user:
        warnings.append("pricing.json has no valid models; using bundled defaults")
        return defaults, warnings

    # User file wins; merge bundled defaults underneath so new models still price.
    merged = dict(defaults)
    merged.update(user)
    return merged, warnings
