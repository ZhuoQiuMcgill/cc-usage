"""Multi-root account discovery for Claude and Codex (T11, T12).

Neither Claude transcripts nor Codex rollouts carry an account identifier
(verified on real data), so the provider **config-dir root** is the only reliable
account boundary. This module discovers those roots — always `~/.claude` /
`~/.codex`, plus an environment override and any `config.json`-declared extra
roots — derives a short label per root, and records which are enabled. Every root
is read-only; nothing here writes under one.

Claude precedence (deduplicated by resolved path):
  1. `~/.claude`            — always (label ``personal``).
  2. `$CLAUDE_CONFIG_DIR`   — when set and distinct.
  3. ``config.json`` key ``claude_roots``.

Codex mirrors it (T12):
  1. `~/.codex`             — always (label ``codex``).
  2. `$CODEX_HOME`          — when set and distinct. NOTE: this *adds* a root; it
     used to replace the default (pre-2.3.x behaviour).
  3. ``config.json`` key ``codex_roots``.

Each root object is ``{"path": ..., "label": ..., "enabled": ...}``. Missing or
unreadable non-default roots are skipped silently (never fatal). Claude and Codex
labels share one namespace so ``a``-key scoping and the by-account rollup can tell
every account apart: Claude reserves the Codex default label and ``all``; Codex
reserves ``all`` plus the live Claude labels, so a codex root never shadows a
Claude account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Provider dimension carried on every UsageRecord. Kept separate from the account
# *label* so a codex root can have any label (``codex``, ``codex-win``, …) while
# scope filtering, the by-account rollup, and the "is any Codex data present" flag
# still recognise it as Codex. (Pre-T12 this rode on ``account == CODEX_ACCOUNT``,
# which broke the moment a second codex root existed.)
CLAUDE_PROVIDER = "claude"
CODEX_PROVIDER = "codex"

# The always-present default labels: ``~/.claude`` -> ``personal``,
# ``~/.codex`` -> ``codex``. ``CODEX_ACCOUNT`` doubles as the default codex label
# and the string a plain single-codex machine renders as ``Codex`` (zero-noise).
DEFAULT_LABEL = "personal"
CODEX_ACCOUNT = "codex"

# The ``all`` scope sentinel: a root literally labelled "all" could never be
# isolated (cycling to it would read as the all-accounts scope), so it is always
# reserved. Claude additionally reserves the Codex default label so the two
# provider label spaces stay disjoint.
_ALL_SCOPE = "all"
_CLAUDE_RESERVED = frozenset({CODEX_ACCOUNT, _ALL_SCOPE})


@dataclass(frozen=True)
class Root:
    """One discovered account root (Claude or Codex).

    ``path`` is the config dir (``…/.claude`` or ``…/.codex``); ``projects`` is
    where its transcripts live — ``<path>/projects`` for Claude, ``<path>/sessions``
    for Codex (the archived sibling is derived from ``path`` by the engine).
    ``source`` is ``auto`` (the default root), ``env`` (the environment override)
    or ``config`` (a declared roots entry).
    """

    label: str
    path: Path
    projects: Path
    source: str
    enabled: bool = True


def _derive_label(path: Path, strip_prefix: str) -> str:
    """Basename with a leading provider prefix or a bare ``.`` stripped.

    ``.claude-rdqcc`` (prefix ``.claude-``) -> ``rdqcc``; ``.codex-win`` (prefix
    ``.codex-``) -> ``win``; ``.codex`` -> ``codex``; a plain dir keeps its name.
    Never returns an empty string.
    """
    name = path.name
    if strip_prefix and name.startswith(strip_prefix):
        name = name[len(strip_prefix) :]
    elif name.startswith("."):
        name = name[1:]
    return name or path.name or "account"


def _dedupe_label(label: str, used: set[str]) -> str:
    """Return ``label`` or a numeric-suffixed variant if it is already taken.

    ``used`` is seeded with the caller's reserved labels so no root can claim the
    ``all`` scope sentinel (or, cross-provider, another account's label).
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


def _discover_roots(
    config,
    *,
    default_path: Path,
    default_label: str,
    env_var: str,
    config_key: str,
    projects_subdir: str,
    strip_prefix: str,
    reserved,
    home: Path,
    environ,
) -> list[Root]:
    """Shared discovery for one provider's roots (Claude and Codex share it all).

    Precedence: the always-present default root, then a distinct ``$env_var``
    override, then each ``config.json`` ``config_key`` entry. Deduplicated by
    resolved path; the default root is always listed (even if its dir does not
    exist yet), every other candidate is skipped when missing. A root is enabled
    unless a config object sets ``enabled: false`` or the user toggled it off.
    ``reserved`` seeds the label space (``all`` plus any cross-provider labels).
    """
    disabled = _disabled_identities(config)

    # (path, source, label_override, config_enabled)
    candidates: list[tuple[Path, str, str | None, bool | None]] = [
        (default_path, "auto", default_label, None),
    ]
    env_dir = environ.get(env_var)
    if env_dir:
        candidates.append((Path(env_dir).expanduser(), "env", None, None))
    for entry in getattr(config, config_key, None) or []:
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
    used_labels: set[str] = set(reserved)
    roots: list[Root] = []
    for path, source, label_override, cfg_enabled in candidates:
        try:
            resolved = str(path.resolve())
        except (OSError, RuntimeError):
            resolved = str(path)
        if resolved in seen:
            continue
        # The default root is always listed; every other root must actually exist.
        if source != "auto" and not path.is_dir():
            continue
        seen.add(resolved)

        if label_override:
            label = label_override
        elif source == "auto":
            label = default_label
        else:
            label = _derive_label(path, strip_prefix)
        label = _dedupe_label(label, used_labels)

        enabled = cfg_enabled is not False and str(path) not in disabled
        roots.append(
            Root(label=label, path=path, projects=path / projects_subdir, source=source, enabled=enabled)
        )
    return roots


def discover_claude_roots(config, *, home: Path | None = None, environ=None) -> list[Root]:
    """Discover Claude account roots in precedence order (see module docstring).

    ``~/.claude`` is always present; ``$CLAUDE_CONFIG_DIR`` and ``claude_roots``
    add more. ``home``/``environ`` are injectable so discovery is unit-testable
    without touching the real ``$HOME`` or process environment.
    """
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    return _discover_roots(
        config,
        default_path=home / ".claude",
        default_label=DEFAULT_LABEL,
        env_var="CLAUDE_CONFIG_DIR",
        config_key="claude_roots",
        projects_subdir="projects",
        strip_prefix=".claude-",
        reserved=_CLAUDE_RESERVED,
        home=home,
        environ=environ,
    )


def discover_codex_roots(
    config, *, claude_roots: list[Root] | None = None, home: Path | None = None, environ=None
) -> list[Root]:
    """Discover Codex account roots, mirroring the Claude model (T12).

    ``~/.codex`` is always present; ``$CODEX_HOME`` (now *additive*, not a
    replacement) and ``codex_roots`` add more. Codex labels are reserved against
    the live Claude labels plus ``all`` so the two providers share one
    unambiguous scope namespace — pass ``claude_roots`` to reuse an
    already-computed set (else it is re-discovered).
    """
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    if claude_roots is None:
        claude_roots = discover_claude_roots(config, home=home, environ=environ)
    reserved = frozenset({_ALL_SCOPE, *(r.label for r in claude_roots)})
    return _discover_roots(
        config,
        default_path=home / ".codex",
        default_label=CODEX_ACCOUNT,
        env_var="CODEX_HOME",
        config_key="codex_roots",
        projects_subdir="sessions",
        strip_prefix=".codex-",
        reserved=reserved,
        home=home,
        environ=environ,
    )
