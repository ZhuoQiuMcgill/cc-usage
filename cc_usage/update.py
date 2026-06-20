"""Self-updater for ccusage (explicit user command — network IS allowed here).

This module backs `ccusage --check-update` and `ccusage --update`. Both are
*explicit* user-invoked commands, so they may reach the network. This is distinct
from the panel/data path, which remains strictly no-network (AGENT_RULEBOOK hard
rule 3): nothing here is ever called from the TUI, the engine, or the scan loop.

Design notes:
  - `latest_release()` and `_pip_install()` are kept as small, separately
    *mockable* functions so the tests can stub the network and pip out entirely.
  - Every failure mode (no network, missing pip, GitHub down, no release yet) is
    caught and turned into a friendly message + a non-zero return — never a
    traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

from . import __version__

REPO = "ZhuoQiuMcgill/cc-usage"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
GIT_URL = "https://github.com/ZhuoQiuMcgill/cc-usage.git"

_TIMEOUT = 10  # seconds


def latest_release() -> str | None:
    """Return the latest GitHub release tag (e.g. ``v2.0.0``) or ``None``.

    Uses only the stdlib (``urllib``) — no new dependency. Returns ``None`` on
    ANY error (no network, timeout, HTTP error, no release published yet, or a
    malformed response) so callers never have to handle exceptions.
    """
    req = urllib.request.Request(
        RELEASES_LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ccusage-self-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    tag = data.get("tag_name")
    if isinstance(tag, str) and tag:
        return tag
    return None


def _normalize(tag: str) -> str:
    """Strip a leading ``v`` so ``v2.0.0`` and ``2.0.0`` compare equal."""
    return tag[1:] if tag.startswith("v") else tag


def is_up_to_date(latest: str | None) -> bool:
    """True if the installed version already matches the latest release tag."""
    if latest is None:
        return False
    return _normalize(latest) == _normalize(__version__)


def _pip_install(tag: str | None) -> int:
    """Invoke pip to upgrade to ``tag`` (or ``@main`` when ``tag`` is None).

    Kept separate and mockable so tests can assert the exact argv WITHOUT ever
    running pip for real. Returns pip's exit code; returns a non-zero code (and
    prints a friendly message) if pip itself is missing.
    """
    ref = tag if tag else "main"
    target = f"git+{GIT_URL}@{ref}"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    try:
        completed = subprocess.run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ccusage: could not run pip ({exc}).")
        print("Install/upgrade manually:")
        print(f"  pip install --upgrade {target}")
        return 1
    return completed.returncode


def check_update() -> int:
    """Backs ``ccusage --check-update``: report current vs latest, install nothing."""
    print(f"ccusage {__version__}")
    latest = latest_release()
    if latest is None:
        print("Could not reach GitHub to check for updates (no release found or no network).")
        return 1
    if is_up_to_date(latest):
        print(f"You are on the latest release ({latest}).")
        return 0
    print(f"An update is available: {latest} (you have {__version__}).")
    print("Run 'ccusage --update' to upgrade.")
    return 0


def perform_update() -> int:
    """Backs ``ccusage --update``: upgrade to the latest release via pip.

    If already on the latest release, says so and makes NO pip call. Otherwise
    runs pip against ``git+...@<tag>`` (falling back to ``@main`` when no release
    is published). All failures degrade to a friendly message + non-zero exit.
    """
    latest = latest_release()
    if is_up_to_date(latest):
        print(f"ccusage is already up to date ({__version__}).")
        return 0

    if latest is None:
        print("No published release found — installing from the latest 'main' instead.")
    else:
        print(f"Updating ccusage {__version__} -> {latest} ...")

    code = _pip_install(latest)
    if code == 0:
        target = latest if latest else "main"
        print(f"ccusage updated successfully ({target}).")
    else:
        print("ccusage update failed. See the pip output above.")
    return code
