"""Statusline install -> restore driven through the in-app Settings screen (T3 R3).

Hermetic: a fake ~/.claude (settings.json + statusline-command.sh) and a fake config
dir are created under tmp_path and every relevant path in statusline.py is monkeypatched
to point there, so the REAL ~/.claude is never touched. We drive the actual Settings ->
Statusline -> Install / Restore keyboard flow and prove (by sha256) that both files come
back byte-identical to their originals — the central safety promise (Guardrail 1).

The install/restore logic itself is the unchanged T2 code; this test proves the new
keyboard path reaches it and the reversibility still holds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import cc_usage.config as cfgmod
import cc_usage.statusline as sl
from cc_usage.app import CCUsageApp
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.settings_screen import ChoiceScreen, ResultScreen, SettingsScreen
from cc_usage.statusline import status
from textual.widgets import ListView

_FAKE_SCRIPT = "#!/usr/bin/env bash\n# fake original statusline\ncat >/dev/null; printf 'STATUS'\n"
_FAKE_SETTINGS = {
    "statusLine": {"type": "command", "command": "bash ~/.claude/statusline-command.sh"},
    "other": {"keep": "me"},
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.mkdir()
    settings = claude / "settings.json"
    script = claude / "statusline-command.sh"
    settings.write_text(json.dumps(_FAKE_SETTINGS, indent=2) + "\n", "utf-8")
    script.write_text(_FAKE_SCRIPT, "utf-8")
    script.chmod(0o755)

    cfgdir = tmp_path / "config"
    backups = cfgdir / "backups"
    cfgdir.mkdir()
    backups.mkdir()

    # Repoint every path statusline.py uses. The wrapper's inner command in the fake
    # settings says `bash ~/.claude/...`; statusline.install expands ~/ -> $HOME/. To
    # keep the proof self-contained we also point $HOME at tmp so ~ resolves here.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sl, "SETTINGS_JSON", settings)
    monkeypatch.setattr(sl, "STATUSLINE_SCRIPT", script)
    monkeypatch.setattr(sl, "SETTINGS_ORIG", backups / "settings.json.orig")
    monkeypatch.setattr(sl, "SCRIPT_ORIG", backups / "statusline-command.sh.orig")
    monkeypatch.setattr(sl, "SETTINGS_PREINSTALL", backups / "settings.json.preinstall")
    monkeypatch.setattr(sl, "WRAPPER_SCRIPT", cfgdir / "statusline-wrapper.sh")
    monkeypatch.setattr(sl, "RATELIMITS_JSON", cfgdir / "ratelimits.json")

    def _ensure_dirs():
        cfgdir.mkdir(exist_ok=True)
        backups.mkdir(exist_ok=True)

    monkeypatch.setattr(sl, "ensure_dirs", _ensure_dirs)
    # config persistence also into tmp
    monkeypatch.setattr(cfgmod, "CONFIG_JSON", cfgdir / "config.json")

    # Symlink so the wrapper's `$HOME/.claude/statusline-command.sh` resolves to our fake.
    (tmp_path / ".claude").symlink_to(claude)

    return {"settings": settings, "script": script}


def test_install_then_restore_via_settings_keyboard(fake_env):
    settings, script = fake_env["settings"], fake_env["script"]
    orig_settings_sha = _sha(settings)
    orig_script_sha = _sha(script)

    async def scenario():
        eng = Engine(Config(), cache_path=None)  # hermetic: never touch the real parse cache
        eng._scanned = True
        app = CCUsageApp(eng)
        async with app.run_test() as pilot:
            # ── Install ──
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            for _ in range(4):  # statusline row is index 4
                await pilot.press("down")
            await pilot.pause()
            assert app.screen.query_one("#settings-list", ListView).index == 4
            await pilot.press("enter")  # open statusline menu
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("enter")  # Install (index 0)
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            await pilot.press("enter")  # dismiss result
            await pilot.pause()

            assert status()["installed"] is True
            # settings repointed, script untouched
            assert _sha(settings) != orig_settings_sha
            assert _sha(script) == orig_script_sha
            assert "wrapper" in json.loads(settings.read_text())["statusLine"]["command"]
            # unrelated keys preserved
            assert json.loads(settings.read_text())["other"] == {"keep": "me"}

            # ── Restore ──
            assert isinstance(app.screen, SettingsScreen)
            for _ in range(4):
                await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")  # open statusline menu
            await pilot.pause()
            await pilot.press("down")  # Restore (index 1)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ResultScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert status()["installed"] is False

    asyncio.run(scenario())

    # ── sha256 proof: both files byte-identical to their originals ──
    assert _sha(settings) == orig_settings_sha, "settings.json not restored byte-identically"
    assert _sha(script) == orig_script_sha, "statusline-command.sh not restored byte-identically"
    # wrapper + caches cleaned up
    assert not sl.WRAPPER_SCRIPT.exists()
    assert not sl.RATELIMITS_JSON.exists()
    assert not sl.SETTINGS_PREINSTALL.exists()
