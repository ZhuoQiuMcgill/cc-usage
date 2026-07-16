"""Filesystem locations. All config/cache lives under XDG ~/.config/cc-usage/.

Provider files are read-only except the explicit legacy statusline restore command.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# Claude Code transcripts and OAuth credentials are read-only.
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
STATUSLINE_SCRIPT = CLAUDE_DIR / "statusline-command.sh"
CLAUDE_CREDENTIALS = CLAUDE_DIR / ".credentials.json"

# Codex / ChatGPT app is always read-only. `$CODEX_HOME` is deliberately NOT read here:
# since T12 it is an *additive* discovery root (see accounts.discover_codex_roots), so
# this default dir — used by the single-default legacy scan path — must always be
# ~/.codex. Baking the env in here would divert that scan to a directory discovery never
# reports (e.g. a missing $CODEX_HOME), so the panel would scan /gone while Settings lists
# ~/.codex.
CODEX_DIR = HOME / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_ARCHIVED_SESSIONS_DIR = CODEX_DIR / "archived_sessions"

# ── Our own config/cache (XDG) ──
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config"))
CONFIG_DIR = XDG_CONFIG_HOME / "cc-usage"
BACKUPS_DIR = CONFIG_DIR / "backups"

PRICING_JSON = CONFIG_DIR / "pricing.json"
CONFIG_JSON = CONFIG_DIR / "config.json"
LIMITS_CACHE_JSON = CONFIG_DIR / "provider-limits.json"
# Legacy statusline artifacts retained only so --restore-statusline can clean them up.
RATELIMITS_JSON = CONFIG_DIR / "ratelimits.json"
WRAPPER_SCRIPT = CONFIG_DIR / "statusline-wrapper.sh"

# Persistent parse cache (M6 extended across process runs). Lets a relaunch read only
# transcript bytes appended since the last run instead of re-parsing every file from
# scratch. Pure derived data — safe to delete at any time; a missing/stale/corrupt cache
# just falls back to a full scan. Never holds anything from ~/.claude beyond parsed usage.
PARSE_CACHE = CONFIG_DIR / "parse-cache.pkl"

# Backups (the .orig pair is created on first capture/install; preinstall snapshots
# are written immediately before the wrapper repoints settings.json).
SETTINGS_ORIG = BACKUPS_DIR / "settings.json.orig"
SCRIPT_ORIG = BACKUPS_DIR / "statusline-command.sh.orig"
SETTINGS_PREINSTALL = BACKUPS_DIR / "settings.json.preinstall"


def ensure_dirs() -> None:
    """Create the config + backups directories if missing (idempotent)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
