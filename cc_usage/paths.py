"""Filesystem locations. All config/cache lives under XDG ~/.config/cc-usage/.

The only ~/.claude files this tool ever *modifies* are the statusline settings/script,
and only reversibly (see statusline.py). Everything else here is read-only.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# ── Claude Code (read-only, except the statusline pair handled reversibly) ──
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
STATUSLINE_SCRIPT = CLAUDE_DIR / "statusline-command.sh"

# ── Our own config/cache (XDG) ──
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config"))
CONFIG_DIR = XDG_CONFIG_HOME / "cc-usage"
BACKUPS_DIR = CONFIG_DIR / "backups"

PRICING_JSON = CONFIG_DIR / "pricing.json"
CONFIG_JSON = CONFIG_DIR / "config.json"
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
