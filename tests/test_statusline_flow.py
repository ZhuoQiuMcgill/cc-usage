"""Legacy statusline integrations remain safely removable."""

import hashlib
import json
from pathlib import Path

import cc_usage.statusline as statusline


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_restore_removes_legacy_capture_and_restores_exact_settings(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    backups = tmp_path / "backups"
    config = tmp_path / "config"
    claude.mkdir()
    backups.mkdir()
    config.mkdir()

    original_settings = (
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": "bash ~/.claude/statusline-command.sh",
                },
                "other": {"keep": "me"},
            },
            indent=2,
        )
        + "\n"
    )
    settings = claude / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": "python -m cc_usage --capture-statusline old",
                },
                "other": {"keep": "me"},
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    preinstall = backups / "settings.json.preinstall"
    preinstall.write_text(original_settings, "utf-8")
    script = claude / "statusline-command.sh"
    script.write_text("printf STATUS\n", "utf-8")
    script_backup = backups / "statusline-command.sh.orig"
    script_backup.write_bytes(script.read_bytes())
    wrapper = config / "statusline-wrapper.sh"
    wrapper.write_text("legacy", "utf-8")
    limits = config / "ratelimits.json"
    limits.write_text("{}", "utf-8")

    monkeypatch.setattr(statusline, "SETTINGS_JSON", settings)
    monkeypatch.setattr(statusline, "SETTINGS_ORIG", backups / "settings.json.orig")
    monkeypatch.setattr(statusline, "SETTINGS_PREINSTALL", preinstall)
    monkeypatch.setattr(statusline, "STATUSLINE_SCRIPT", script)
    monkeypatch.setattr(statusline, "SCRIPT_ORIG", script_backup)
    monkeypatch.setattr(statusline, "WRAPPER_SCRIPT", wrapper)
    monkeypatch.setattr(statusline, "RATELIMITS_JSON", limits)
    monkeypatch.setattr(statusline, "ensure_dirs", lambda: None)

    expected_settings_sha = _sha(preinstall)
    expected_script_sha = _sha(script)
    result = statusline.restore()

    assert result["ok"] is True
    assert _sha(settings) == expected_settings_sha
    assert _sha(script) == expected_script_sha
    assert statusline.status()["installed"] is False
    assert not wrapper.exists()
    assert not limits.exists()
