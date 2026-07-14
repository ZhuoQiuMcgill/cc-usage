"""Multi-account Claude transcript roots (T11).

Claude transcripts carry no account identifier (verified on real data), so the
Claude **config-dir root** is the only reliable account boundary. This module
discovers those roots — always `~/.claude`, plus a `$CLAUDE_CONFIG_DIR` override
and any `config.json`-declared extra roots — derives a short label per root, and
records which are enabled. Every root is read-only; nothing here writes under one.

Precedence (deduplicated by resolved path):
  1. `~/.claude`            — always (label ``personal``).
  2. `$CLAUDE_CONFIG_DIR`   — when set and distinct.
  3. ``config.json`` key ``claude_roots`` — a list of
     ``{"path": ..., "label": ..., "enabled": ...}`` objects.

Missing/unreadable non-default roots are skipped silently (never fatal).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Sentinel account tag for Codex records. Codex is a provider, never a Claude
# account, but tagging its records with a distinct account string lets scope
# filtering and the by-account rollup treat it as one row without threading a
# separate provider field through every record. Reserved so no Claude label can
# collide with it (see `_dedupe_label`).
CODEX_ACCOUNT = "codex"

# The always-present default root's label (``~/.claude``).
DEFAULT_LABEL = "personal"


@dataclass(frozen=True)
class Root:
    """One discovered Claude account root.

    ``path`` is the config dir (``…/.claude``); ``projects`` is where its
    transcripts live. ``source`` is ``auto`` (``~/.claude``), ``env``
    (``$CLAUDE_CONFIG_DIR``) or ``config`` (a ``claude_roots`` entry).
    """

    label: str
    path: Path
    projects: Path
    source: str
    enabled: bool = True


def _derive_label(path: Path) -> str:
    """Basename with a leading ``.claude-`` or ``.`` stripped.

    ``.claude-rdqcc`` -> ``rdqcc``; ``.claude`` -> ``claude``; a plain dir keeps
    its name. Never returns an empty string.
    """
    name = path.name
    if name.startswith(".claude-"):
        name = name[len(".claude-") :]
    elif name.startswith("."):
        name = name[1:]
    return name or path.name or "account"


def _dedupe_label(label: str, used: set[str]) -> str:
    """Return ``label`` or a numeric-suffixed variant if it is already taken.

    ``used`` is seeded with ``CODEX_ACCOUNT`` by the caller so no Claude root can
    claim the reserved Codex tag.
    """
    if label not in used:
        used.add(label)
        return label
    i = 2
    while f"{label}-{i}" in used:
        i += 1
    new = f"{label}-{i}"
    used.add(new)
    return new


def _disabled_identities(config) -> set[str]:
    """Path strings the user has toggled off in the settings screen."""
    out: set[str] = set()
    for raw in getattr(config, "disabled_roots", None) or []:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            out.add(str(Path(raw).expanduser()))
        except (TypeError, ValueError):
            continue
    return out


def discover_claude_roots(config, *, home: Path | None = None, environ=None) -> list[Root]:
    """Discover Claude account roots in precedence order (see module docstring).

    Deduplicated by resolved path. ``~/.claude`` is always present (even if it
    does not exist yet); every other candidate is skipped when its directory is
    missing. A root is enabled unless a ``config.json`` object sets
    ``enabled: false`` or the user toggled it off (``disabled_roots``).

    ``home``/``environ`` are injectable so discovery is unit-testable without
    touching the real ``$HOME`` or process environment.
    """
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    disabled = _disabled_identities(config)

    # (path, source, label_override, config_enabled)
    candidates: list[tuple[Path, str, str | None, bool | None]] = [
        (home / ".claude", "auto", DEFAULT_LABEL, None),
    ]
    env_dir = environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        candidates.append((Path(env_dir).expanduser(), "env", None, None))
    for entry in getattr(config, "claude_roots", None) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        label_override = entry.get("label")
        label_override = label_override if isinstance(label_override, str) and label_override else None
        cfg_enabled = entry.get("enabled")
        cfg_enabled = cfg_enabled if isinstance(cfg_enabled, bool) else None
        candidates.append((Path(raw).expanduser(), "config", label_override, cfg_enabled))

    seen: set[str] = set()
    used_labels: set[str] = {CODEX_ACCOUNT}
    roots: list[Root] = []
    for path, source, label_override, cfg_enabled in candidates:
        try:
            resolved = str(path.resolve())
        except (OSError, RuntimeError):
            resolved = str(path)
        if resolved in seen:
            continue
        # ~/.claude is always listed; every other root must actually exist.
        if source != "auto" and not path.is_dir():
            continue
        seen.add(resolved)

        if label_override:
            label = label_override
        elif source == "auto":
            label = DEFAULT_LABEL
        else:
            label = _derive_label(path)
        label = _dedupe_label(label, used_labels)

        enabled = cfg_enabled is not False and str(path) not in disabled
        roots.append(
            Root(label=label, path=path, projects=path / "projects", source=source, enabled=enabled)
        )
    return roots
