"""Self-updater for ccusage (explicit user command — network IS allowed here).

This module backs `ccusage --check-update` and `ccusage --update`. Both are
*explicit* user-invoked commands, so they may reach the network. This is distinct
from the panel/data path, which remains strictly no-network (AGENT_RULEBOOK hard
rule 3): nothing here is ever called from the TUI, the engine, or the scan loop.

Design notes:
  - `latest_release()`, `_pip_install()`, and `_uv_install()` are kept as small,
    separately *mockable* functions so the tests can stub the network, pip, and
    uv out entirely.
  - A `uv tool install` environment has no pip. `_should_use_uv()` detects that
    (no pip, but `uv` on PATH) and `_install()` routes to `_uv_install()`
    instead, so the built-in commands upgrade in place there too instead of
    failing with a "could not run pip" dead end.
  - On Windows, upgrading ccusage while ccusage itself is doing the upgrading
    means its own launcher `.exe` is open, so pip/uv can install the new
    package but can't refresh the entry point — Windows refuses to overwrite a
    running executable's image (Unix has no such restriction).
    `_is_windows_self_replace_error()` recognizes that specific failure and
    `_run_install()` prints `_SELF_REPLACE_HINT` instead of leaving a bare
    Win32 error on screen.
  - Every failure mode (no network, missing pip/uv, GitHub down, no release
    yet, the Windows self-replace case above) is caught and turned into a
    friendly message + a non-zero return — never a traceback.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from . import __version__

REPO = "ZhuoQiuMcgill/cc-usage"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"
GIT_URL = "https://github.com/ZhuoQiuMcgill/cc-usage.git"
TOOL_NAME = "cc-usage"  # the uv tool / pipx install name (pyproject `[project].name`)

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


def _pip_available() -> bool:
    """True if the ``pip`` module can be imported in this interpreter.

    A ``uv tool install`` environment is isolated and deliberately ships without
    pip, so this is ``False`` there even though the interpreter itself works fine.
    """
    return importlib.util.find_spec("pip") is not None


def _uv_executable() -> str | None:
    """Return the path to a ``uv`` executable on ``PATH``, or ``None``."""
    return shutil.which("uv")


def _should_use_uv() -> bool:
    """True when this looks like a ``uv tool`` install: no pip, but ``uv`` itself
    is reachable on ``PATH``.

    Kept separate and mockable (like ``latest_release()`` / ``_pip_install()``)
    so tests can force either branch without touching the real interpreter or
    ``PATH``.
    """
    return _uv_executable() is not None and not _pip_available()


def _is_windows_self_replace_error(output: str) -> bool:
    """True if ``output`` looks like Windows refusing to overwrite ccusage's own
    running executable (a Win32 sharing violation, error 32).

    Upgrading ccusage while ccusage itself is the process driving the upgrade
    means its own launcher ``.exe`` is open — Windows (unlike Unix, which allows
    replacing a running binary on disk) refuses to overwrite the image of a
    running executable, so the final entry-point step fails even though the
    underlying package install has usually already succeeded by that point. Pip
    and uv both surface this via the OS's own canned message rather than
    wording of their own, so matching that phrase (gated on ``sys.platform``)
    catches it from either backend without depending on either tool's exact
    output format.
    """
    return sys.platform == "win32" and "being used by another process" in output


_SELF_REPLACE_HINT = (
    "ccusage: this looks like Windows refusing to replace its own running "
    "executable (a running .exe can't be overwritten on Windows; Unix has no "
    "such restriction). The underlying package is usually updated anyway; "
    "check with 'ccusage --version'. To refresh the command too, close any "
    "other running ccusage windows and re-run, or run it once via "
    "'python -m cc_usage' in place of 'ccusage'."
)


def _run_install(cmd: list[str], tool: str, manual: str) -> int:
    """Run an install/upgrade ``cmd`` for ``tool`` (``"pip"`` or ``"uv"``), print
    its output, and return its exit code.

    Shared by ``_pip_install()`` and ``_uv_install()``. Captures combined
    stdout/stderr (rather than letting them stream live) so a failed run can be
    checked for the Windows self-replace error above and given
    ``_SELF_REPLACE_HINT`` instead of leaving a bare Win32 error on screen; the
    captured text is still printed in full either way. If ``tool`` itself
    cannot be run at all, prints a friendly message + the manual fallback
    command and returns 1.
    """
    try:
        completed = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ccusage: could not run {tool} ({exc}).")
        print("Install/upgrade manually:")
        print(f"  {manual}")
        return 1
    print(completed.stdout, end="")
    if completed.returncode != 0 and _is_windows_self_replace_error(completed.stdout):
        print(_SELF_REPLACE_HINT)
    return completed.returncode


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
    manual = f"pip install --upgrade {'--force-reinstall ' if force else ''}{target}"
    return _run_install(cmd, "pip", manual)


def _uv_install(tag: str | None, force: bool = False) -> int:
    """Upgrade/install ``tag`` (or ``@main``) via ``uv`` in a uv-tool environment.

    Mirrors ``_pip_install()`` for the pip-less environment ``uv tool install``
    creates. A plain upgrade (``force=False``) runs ``uv tool upgrade``, the same
    command the README already tells uv users to run by hand — it ignores
    ``tag`` since uv re-resolves the tool's existing source itself. A pinned
    install (``force=True``, used by the test-channel and ``--update-stable``
    commands) instead runs ``uv tool install --force git+...@<ref>`` so a build
    sharing the installed ``__version__`` still actually switches.

    Returns uv's exit code; returns a non-zero code (and prints a friendly
    message) if ``uv`` itself cannot be run.
    """
    if force:
        ref = tag if tag else "main"
        cmd = ["uv", "tool", "install", "--force", f"git+{GIT_URL}@{ref}"]
    else:
        cmd = ["uv", "tool", "upgrade", TOOL_NAME]
    return _run_install(cmd, "uv", " ".join(cmd))


def _install(tag: str | None, force: bool = False) -> int:
    """Upgrade to ``tag`` (or ``@main``), routing around a missing pip.

    pipx and pip-in-a-venv installs have pip and use ``_pip_install()``
    unchanged. A ``uv tool install`` environment has none — ``_should_use_uv()``
    detects that and this delegates to ``_uv_install()`` instead, so every
    ``--update*`` command works under all three supported installers.
    """
    if _should_use_uv():
        return _uv_install(tag, force=force)
    return _pip_install(tag, force=force)


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
    """Backs ``ccusage --update``: upgrade to the latest release.

    If already on the latest release, says so and makes NO install call.
    Otherwise upgrades to ``git+...@<tag>`` (falling back to ``@main`` when no
    release is published) via pip, or via ``uv tool upgrade`` when this is a
    pip-less uv-tool install (see ``_install()``). All failures degrade to a
    friendly message + non-zero exit.
    """
    latest = latest_release()
    if is_up_to_date(latest):
        print(f"ccusage is already up to date ({__version__}).")
        return 0

    if latest is None:
        print("No published release found — installing from the latest 'main' instead.")
    else:
        print(f"Updating ccusage {__version__} -> {latest} ...")

    code = _install(latest)
    if code == 0:
        target = latest if latest else "main"
        print(f"ccusage updated successfully ({target}).")
    else:
        print("ccusage update failed. See the output above.")
    return code


def perform_update_pr(n: int) -> int:
    """Backs ``ccusage --update-pr <N>``: install the head of open PR ``N``.

    This is a **test build** of UNREVIEWED code from a pull request — the caution
    is printed up front. ``N`` must be a positive integer; ``<= 0`` is rejected
    with a friendly message and a non-zero exit, making NO install call. On success it
    prints how to return to the official release. Force-reinstall is required so a
    PR build that shares the stable ``__version__`` still actually installs.
    """
    if n <= 0:
        print(f"ccusage: invalid PR number {n!r} — expected a positive integer.")
        return 1

    ref = pr_ref(n)
    print(f"Installing PR #{n} ({ref}) for testing ...")
    print("Caution: this installs UNREVIEWED code from a pull request.")
    code = _install(ref, force=True)
    if code == 0:
        print(f"Installed PR #{n} for testing.")
        print("Return to the official release with: ccusage --update-stable")
    else:
        print("ccusage PR install failed. See the output above.")
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

    code = _install(tag, force=True)
    if code == 0:
        target = tag if tag else "main"
        print(f"Installed prerelease build ({target}).")
        print("Return to the official release with: ccusage --update-stable")
    else:
        print("ccusage prerelease install failed. See the output above.")
    return code


def perform_update_stable() -> int:
    """Backs ``ccusage --update-stable``: return to the latest official release.

    Force-reinstalls the latest stable release tag (``latest_release()``) so a
    machine currently on a PR/prerelease test build is restored to the official
    release even when the version strings match. If no stable release is
    published, reports it and exits non-zero (NO install call).
    """
    latest = latest_release()
    if latest is None:
        print("No published release found — cannot return to a stable release.")
        return 1

    print(f"Returning to the official release {latest} ...")
    code = _install(latest, force=True)
    if code == 0:
        print(f"ccusage restored to the official release ({latest}).")
    else:
        print("ccusage update failed. See the output above.")
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
