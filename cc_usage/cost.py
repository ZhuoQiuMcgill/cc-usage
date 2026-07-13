"""Cost model (T0 §6) — exact, and tolerant of unknown model ids.

cost =  input_tokens              * input_rate
      + output_tokens             * output_rate
      + cache_read_input_tokens   * cache_read_rate
      + ephemeral_5m_input_tokens * input_rate * 1.25
      + ephemeral_1h_input_tokens * input_rate * 2.00

If a row omits cache_read_rate, it defaults to input_rate * 0.10. If the
cache_creation.ephemeral_* sub-buckets are absent, creation falls back to
cache_creation_input_tokens * input_rate * 1.25.

Rates are USD per 1,000,000 tokens. Unknown models use a zero arithmetic contribution
while the record's `known` flag preserves that their cost is unavailable; renderers show
them as unpriced rather than free.
Rows may provide explicit cache rates and long-context multipliers. Older editable
pricing files with only ``input`` and ``output`` remain fully compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CACHE_READ_MULT = 0.10
EPHEMERAL_5M_MULT = 1.25
EPHEMERAL_1H_MULT = 2.00
CACHE_CREATE_FALLBACK_MULT = 1.25

_DATE_SUFFIX = re.compile(r"-\d{6,8}$")  # e.g. -20251001
_HYPHENATED_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")  # e.g. -2026-03-05
_ONE_M_SUFFIX = re.compile(r"\[\s*1m\s*\]", re.IGNORECASE)  # e.g. claude-opus-4-8[1m]
_OFFICIAL_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


@dataclass(frozen=True)
class Rates:
    """One model's standard API-equivalent token rates."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long_context_threshold: int | None = None
    long_context_input_multiplier: float = 1.0
    long_context_output_multiplier: float = 1.0


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
    m = _HYPHENATED_DATE_SUFFIX.sub("", m)
    m = _DATE_SUFFIX.sub("", m)
    return m.strip()


def get_rates(
    model: str | None, pricing: dict[str, dict[str, float]]
) -> Rates | None:
    """Return the per-1M rate card for ``model``, or ``None`` if unknown."""
    norm = normalize_model(model)
    if not norm:
        return None
    row = pricing.get(norm)
    if row is None:
        row = pricing.get(_OFFICIAL_ALIASES.get(norm, ""))
    if row is None:
        return None
    return Rates(
        input=row["input"],
        output=row["output"],
        cache_read=row.get("cache_read"),
        cache_write=row.get("cache_write"),
        long_context_threshold=(
            int(row["long_context_threshold"])
            if row.get("long_context_threshold") is not None
            else None
        ),
        long_context_input_multiplier=row.get("long_context_input_multiplier", 1.0),
        long_context_output_multiplier=row.get("long_context_output_multiplier", 1.0),
    )


def compute_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation_total: int,
    ephemeral_5m: int | None,
    ephemeral_1h: int | None,
    rates: Rates | tuple[float, float] | None,
) -> float:
    """Priced dollar contribution for one usage record; missing rates contribute 0.0.

    Callers retain rate availability separately, so this arithmetic identity is never
    presented to users as a real zero-dollar price.

    `ephemeral_5m`/`ephemeral_1h` are None only when the `cache_creation` object
    was absent from the payload; in that case we fall back to the aggregate
    `cache_creation_total * 1.25`.
    """
    if rates is None:
        return 0.0
    # Keep accepting the old two-tuple for callers and plugins built against the
    # original public helper.
    if isinstance(rates, tuple):
        card = Rates(input=rates[0], output=rates[1])
    else:
        card = rates

    long_context = (
        card.long_context_threshold is not None
        and input_tokens + cache_read > card.long_context_threshold
    )
    input_mult = card.long_context_input_multiplier if long_context else 1.0
    output_mult = card.long_context_output_multiplier if long_context else 1.0

    input_rate = card.input * input_mult
    output_rate = card.output * output_mult
    ir = input_rate / 1_000_000.0
    orr = output_rate / 1_000_000.0

    cost = input_tokens * ir + output_tokens * orr
    cache_read_rate = (
        card.cache_read * input_mult
        if card.cache_read is not None
        else input_rate * CACHE_READ_MULT
    )
    cost += cache_read * cache_read_rate / 1_000_000.0

    if card.cache_write is not None:
        cost += cache_creation_total * card.cache_write * input_mult / 1_000_000.0
    elif ephemeral_5m is None and ephemeral_1h is None:
        # sub-buckets absent -> fall back to the aggregate creation count
        cost += cache_creation_total * ir * CACHE_CREATE_FALLBACK_MULT
    else:
        cost += (ephemeral_5m or 0) * ir * EPHEMERAL_5M_MULT
        cost += (ephemeral_1h or 0) * ir * EPHEMERAL_1H_MULT
    return cost
