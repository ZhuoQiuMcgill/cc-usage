"""Cost model (T0 §6) — exact, and tolerant of unknown model ids.

cost =  input_tokens              * input_rate
      + output_tokens             * output_rate
      + cache_read_input_tokens   * input_rate * 0.10
      + ephemeral_5m_input_tokens * input_rate * 1.25
      + ephemeral_1h_input_tokens * input_rate * 2.00

If the cache_creation.ephemeral_* sub-buckets are absent, fall back to
cache_creation_input_tokens * input_rate * 1.25.

Rates are USD per 1,000,000 tokens. Unknown model -> cost 0 and flagged (never crash).
"""

from __future__ import annotations

import re

CACHE_READ_MULT = 0.10
EPHEMERAL_5M_MULT = 1.25
EPHEMERAL_1H_MULT = 2.00
CACHE_CREATE_FALLBACK_MULT = 1.25

_DATE_SUFFIX = re.compile(r"-\d{6,8}$")  # e.g. -20251001
_ONE_M_SUFFIX = re.compile(r"\[\s*1m\s*\]", re.IGNORECASE)  # e.g. claude-opus-4-8[1m]


def normalize_model(model: str | None) -> str:
    """Lower-case and strip tolerant suffixes so transcript ids match pricing keys.

    Strips a `[1m]` context-window marker and a trailing `-YYYYMMDD` date stamp,
    plus any provider prefix like `us.anthropic.` / `anthropic/`.
    """
    if not model:
        return ""
    m = model.strip().lower()
    m = _ONE_M_SUFFIX.sub("", m).strip()
    # provider prefixes occasionally seen in routed ids
    for prefix in ("us.anthropic.", "eu.anthropic.", "anthropic.", "anthropic/"):
        if m.startswith(prefix):
            m = m[len(prefix) :]
    m = _DATE_SUFFIX.sub("", m)
    return m.strip()


def get_rates(
    model: str | None, pricing: dict[str, dict[str, float]]
) -> tuple[float, float] | None:
    """Return (input_rate, output_rate) per-1M for `model`, or None if unknown."""
    norm = normalize_model(model)
    if not norm:
        return None
    row = pricing.get(norm)
    if row is None:
        return None
    return row["input"], row["output"]


def compute_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation_total: int,
    ephemeral_5m: int | None,
    ephemeral_1h: int | None,
    rates: tuple[float, float] | None,
) -> float:
    """Dollar cost for one usage record. Unknown model (rates None) -> 0.0.

    `ephemeral_5m`/`ephemeral_1h` are None only when the `cache_creation` object
    was absent from the payload; in that case we fall back to the aggregate
    `cache_creation_total * 1.25`.
    """
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    ir = input_rate / 1_000_000.0
    orr = output_rate / 1_000_000.0

    cost = input_tokens * ir + output_tokens * orr
    cost += cache_read * ir * CACHE_READ_MULT

    if ephemeral_5m is None and ephemeral_1h is None:
        # sub-buckets absent -> fall back to the aggregate creation count
        cost += cache_creation_total * ir * CACHE_CREATE_FALLBACK_MULT
    else:
        cost += (ephemeral_5m or 0) * ir * EPHEMERAL_5M_MULT
        cost += (ephemeral_1h or 0) * ir * EPHEMERAL_1H_MULT
    return cost
