"""Legacy statusline cleanup.

Current ccusage versions fetch provider limits directly and never install a statusline.
This module remains only so users of older releases can restore their original Claude
settings with the hidden restore-statusline command.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .paths import (
    RATELIMITS_JSON,
    SCRIPT_ORIG,
    SETTINGS_JSON,
    SETTINGS_ORIG,
    SETTINGS_PREINSTALL,
    STATUSLINE_SCRIPT,
    WRAPPER_SCRIPT,
    ensure_dirs,
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_JSON.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _current_command(settings: dict) -> str:
    statusline = settings.get("statusLine")
    if not isinstance(statusline, dict):
        return ""
    command = statusline.get("command")
    return command if isinstance(command, str) else ""


def _is_ccusage_capture(command: str) -> bool:
    return any(
        marker in command
        for marker in ("--capture-statusline", "cc-usage", "statusline-wrapper")
    )


def restore() -> dict:
    """Restore settings saved by an older ccusage statusline integration."""
    ensure_dirs()
    method = None
    if SETTINGS_PREINSTALL.exists():
        shutil.copy2(SETTINGS_PREINSTALL, SETTINGS_JSON)
        method = "preinstall-snapshot"
    elif SETTINGS_ORIG.exists():
        try:
            original = json.loads(SETTINGS_ORIG.read_text("utf-8"))
            current = _read_settings()
            if isinstance(original, dict) and "statusLine" in original:
                current["statusLine"] = original["statusLine"]
            else:
                current.pop("statusLine", None)
            tmp = SETTINGS_JSON.with_suffix(".json.ccusage.tmp")
            tmp.write_text(json.dumps(current, indent=2) + "\n", "utf-8")
            tmp.replace(SETTINGS_JSON)
            method = "orig-statusline-block"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "action": "restore",
                "error": f"could not restore settings: {exc}",
            }
    else:
        return {
            "ok": False,
            "action": "restore",
            "error": "no legacy statusline backup found",
        }

    WRAPPER_SCRIPT.unlink(missing_ok=True)
    RATELIMITS_JSON.unlink(missing_ok=True)
    settings_sha = _sha256(SETTINGS_JSON)
    script_sha = _sha256(STATUSLINE_SCRIPT)
    settings_target = _sha256(SETTINGS_PREINSTALL) or _sha256(SETTINGS_ORIG)
    script_target = _sha256(SCRIPT_ORIG)
    SETTINGS_PREINSTALL.unlink(missing_ok=True)
    settings_match = settings_sha is not None and settings_sha == settings_target
    script_match = script_target is None or script_sha == script_target
    return {
        "ok": settings_match and script_match,
        "action": "restore",
        "method": method,
        "settings_sha": settings_sha,
        "settings_target": settings_target,
        "settings_match": settings_match,
        "script_sha": script_sha,
        "script_target": script_target,
        "script_match": script_match,
    }


def status() -> dict:
    """Report whether a legacy ccusage capture is still configured."""
    return {
        "installed": _is_ccusage_capture(_current_command(_read_settings())),
        "wrapper_exists": WRAPPER_SCRIPT.exists(),
        "ratelimits_exists": RATELIMITS_JSON.exists(),
    }


def format_result(result: dict) -> str:
    if not result.get("ok"):
        return f"✗ restore failed: {result.get('error', 'unknown error')}"
    return "\n".join(
        [
            "✓ legacy statusline integration removed.",
            f"  • method: {result.get('method')}",
            f"  • settings.json matches original: {result.get('settings_match')} "
            f"({(result.get('settings_sha') or '')[:12]}…)",
            f"  • statusline-command.sh matches original: {result.get('script_match')} "
            f"({(result.get('script_sha') or '')[:12]}…)",
        ]
    )
