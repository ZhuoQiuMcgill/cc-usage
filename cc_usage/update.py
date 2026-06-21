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
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"
GIT_URL = "https://github.com/ZhuoQiuMcgill/cc-usage.git"

_TIMEOUT = 10  # seconds


def _get_json(url: str):
    """GET ``url`` and return the decoded JSON, or ``None`` on ANY failure.

    Shared helper for the release resolvers below. Uses only the stdlib
    (``urllib``) — no new dependency. Returns ``None`` on no network, timeout,
    HTTP error, or a malformed body, so callers never have to handle exceptions.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ccusage-self-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def latest_release() -> str | None:
    """Return the latest **stable** GitHub release tag (e.g. ``v2.0.0``) or ``None``.

    Returns ``None`` on ANY error (no network, timeout, HTTP error, no release
    published yet, or a malformed response) so callers never have to handle
    exceptions.
    """
    data = _get_json(RELEASES_LATEST_URL)
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    if isinstance(tag, str) and tag:
        return tag
    return None


def prerelease_release() -> str | None:
    """Return the newest **prerelease** GitHub release tag, or ``None``.

    GETs ``/repos/<REPO>/releases`` (which GitHub returns newest-first) and
    returns the ``tag_name`` of the first entry whose ``prerelease`` is true,
    skipping stable releases. Returns ``None`` on any error, an empty/malformed
    response, or when no prerelease is published — mirroring ``latest_release()``
    so callers never see an exception.
    """
    data = _get_json(RELEASES_URL)
    if not isinstance(data, list):
        return None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("prerelease") is True:
            tag = entry.get("tag_name")
            if isinstance(tag, str) and tag:
                return tag
    return None


def pr_ref(n: int) -> str:
    """Return the git ref GitHub exposes for the head of pull request ``n``."""
    return f"refs/pull/{n}/head"


def _normalize(tag: str) -> str:
    """Strip a leading ``v`` so ``v2.0.0`` and ``2.0.0`` compare equal."""
    return tag[1:] if tag.startswith("v") else tag


def is_up_to_date(latest: str | None) -> bool:
    """True if the installed version already matches the latest release tag."""
    if latest is None:
        return False
    return _normalize(latest) == _normalize(__version__)


def _pip_install(tag: str | None, force: bool = False) -> int:
    """Invoke pip to upgrade to ``tag`` (or ``@main`` when ``tag`` is None).

    Kept separate and mockable so tests can assert the exact argv WITHOUT ever
    running pip for real. Returns pip's exit code; returns a non-zero code (and
    prints a friendly message) if pip itself is missing.

    ``tag`` may be a release tag (``v2.1.0``), a plain branch (``main``), or a
    full ref such as ``refs/pull/2/head`` — it is appended after ``@`` verbatim.
    When ``force`` is true, ``--force-reinstall`` is added so pip re-installs even
    when the target shares the installed ``__version__`` (test builds and stable
    builds can carry the same version string). The plain ``--update`` path keeps
    ``force=False`` and stays unchanged.
    """
    ref = tag if tag else "main"
    target = f"git+{GIT_URL}@{ref}"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if force:
        cmd.append("--force-reinstall")
    cmd.append(target)
    try:
        completed = subprocess.run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ccusage: could not run pip ({exc}).")
        print("Install/upgrade manually:")
        manual = f"pip install --upgrade {'--force-reinstall ' if force else ''}{target}"
        print(f"  {manual}")
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


def perform_update_pr(n: int) -> int:
    """Backs ``ccusage --update-pr <N>``: install the head of open PR ``N``.

    This is a **test build** of UNREVIEWED code from a pull request — the caution
    is printed up front. ``N`` must be a positive integer; ``<= 0`` is rejected
    with a friendly message and a non-zero exit, making NO pip call. On success it
    prints how to return to the official release. Force-reinstall is required so a
    PR build that shares the stable ``__version__`` still actually installs.
    """
    if n <= 0:
        print(f"ccusage: invalid PR number {n!r} — expected a positive integer.")
        return 1

    ref = pr_ref(n)
    print(f"Installing PR #{n} ({ref}) for testing ...")
    print("Caution: this installs UNREVIEWED code from a pull request.")
    code = _pip_install(ref, force=True)
    if code == 0:
        print(f"Installed PR #{n} for testing.")
        print("Return to the official release with: ccusage --update-stable")
    else:
        print("ccusage PR install failed. See the pip output above.")
    return code


def perform_update_prerelease() -> int:
    """Backs ``ccusage --update-prerelease``: install the newest prerelease.

    Installs the latest **prerelease** GitHub release (force-reinstall). If none
    is published, falls back to ``@main`` (also force-reinstall). All failures
    degrade to a friendly message + non-zero exit.
    """
    tag = prerelease_release()
    if tag is None:
        print("No prerelease found — installing from the latest 'main' instead.")
    else:
        print(f"Installing prerelease {tag} for testing ...")

    code = _pip_install(tag, force=True)
    if code == 0:
        target = tag if tag else "main"
        print(f"Installed prerelease build ({target}).")
        print("Return to the official release with: ccusage --update-stable")
    else:
        print("ccusage prerelease install failed. See the pip output above.")
    return code


def perform_update_stable() -> int:
    """Backs ``ccusage --update-stable``: return to the latest official release.

    Force-reinstalls the latest stable release tag (``latest_release()``) so a
    machine currently on a PR/prerelease test build is restored to the official
    release even when the version strings match. If no stable release is
    published, reports it and exits non-zero (NO pip call).
    """
    latest = latest_release()
    if latest is None:
        print("No published release found — cannot return to a stable release.")
        return 1

    print(f"Returning to the official release {latest} ...")
    code = _pip_install(latest, force=True)
    if code == 0:
        print(f"ccusage restored to the official release ({latest}).")
    else:
        print("ccusage update failed. See the pip output above.")
    return code


def check_prerelease() -> int:
    """Backs ``ccusage --check-prerelease``: report installed vs latest prerelease.

    Installs NOTHING. Reports the installed version and the latest prerelease tag
    (or that none exists / GitHub was unreachable).
    """
    print(f"ccusage {__version__}")
    tag = prerelease_release()
    if tag is None:
        print("No prerelease found (or GitHub could not be reached).")
        return 0
    print(f"Latest prerelease: {tag} (you have {__version__}).")
    print("Run 'ccusage --update-prerelease' to install it.")
    return 0
