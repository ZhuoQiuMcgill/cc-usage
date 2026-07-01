"""Self-updater tests (T5).

Hermetic by design — NEVER hits the network and NEVER runs pip for real:
  - `latest_release()` is monkeypatched to return a fixed tag (or None).
  - the pip invocation (`subprocess.run`) is monkeypatched to record the argv.

We assert the three contractual paths:
  (a) up-to-date  -> NO pip call,
  (b) newer       -> pip called with the right `git+...@<tag>` target,
  (c) net failure -> a friendly message, no crash, non-zero exit.
"""

from __future__ import annotations

import cc_usage.update as upd
from cc_usage import __version__


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_check_update_up_to_date_makes_no_pip_call(monkeypatch, capsys):
    """--check-update on the latest release: reports up-to-date, never installs."""
    monkeypatch.setattr(upd, "latest_release", lambda: f"v{__version__}")

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked on --check-update")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.check_update()
    out = capsys.readouterr().out
    assert rc == 0
    assert "latest release" in out.lower()


def test_perform_update_up_to_date_makes_no_pip_call(monkeypatch, capsys):
    """(a) Already latest: perform_update() returns 0 and DOES NOT call pip."""
    monkeypatch.setattr(upd, "latest_release", lambda: f"v{__version__}")

    calls: list[list[str]] = []

    def _record(cmd, *a, **k):  # pragma: no cover - asserts it's never called
        calls.append(cmd)
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update()
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []  # no pip call at all
    assert "already up to date" in out.lower()


def test_perform_update_newer_calls_pip_with_tag(monkeypatch, capsys):
    """(b) A newer release: pip is called with `git+...@<tag>` and --upgrade."""
    newer = "v9.9.9"
    monkeypatch.setattr(upd, "latest_release", lambda: newer)

    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update()
    out = capsys.readouterr().out
    assert rc == 0

    cmd = captured["cmd"]
    # python -m pip install --upgrade git+https://...@v9.9.9
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "--upgrade" in cmd
    target = cmd[-1]
    assert target.startswith("git+https://github.com/ZhuoQiuMcgill/cc-usage.git@")
    assert target.endswith("@" + newer)
    assert "updated successfully" in out.lower()


def test_perform_update_no_release_falls_back_to_main(monkeypatch):
    """No published release (latest is None) -> pip targets @main, not a tag."""
    monkeypatch.setattr(upd, "latest_release", lambda: None)

    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update()
    assert rc == 0
    assert captured["cmd"][-1].endswith("@main")


def test_check_update_network_failure_is_friendly(monkeypatch, capsys):
    """(c) latest_release() returns None (network down): friendly msg, non-zero, no crash."""
    monkeypatch.setattr(upd, "latest_release", lambda: None)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked on a check")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.check_update()
    out = capsys.readouterr().out
    assert rc != 0
    assert "could not reach github" in out.lower()


def test_latest_release_swallows_network_errors(monkeypatch):
    """latest_release() must return None (never raise) on any urlopen failure."""
    import urllib.error

    def _raise(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(upd.urllib.request, "urlopen", _raise)
    assert upd.latest_release() is None


def test_latest_release_parses_tag(monkeypatch):
    """latest_release() returns the tag_name from a well-formed GitHub response."""
    import io
    import json

    class _Resp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    payload = json.dumps({"tag_name": "v2.0.0"}).encode("utf-8")
    monkeypatch.setattr(
        upd.urllib.request, "urlopen", lambda *a, **k: _Resp(payload)
    )
    assert upd.latest_release() == "v2.0.0"
    # io imported to keep the dependency obvious for readers
    assert io is not None


# --------------------------------------------------------------------------- #
# T8 — test-channel commands (--update-pr / --update-prerelease /
# --update-stable / --check-prerelease). Same hermetic contract: NEVER hit the
# network, NEVER run pip for real.
# --------------------------------------------------------------------------- #


class _JSONResp:
    """Minimal urlopen() stand-in returning a fixed JSON body."""

    def __init__(self, payload) -> None:
        import json as _json

        self._payload = _json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- _pip_install: the --force-reinstall wiring --------------------------- #


def test_pip_install_force_appends_force_reinstall(monkeypatch):
    """force=True adds --force-reinstall; the git target is still last."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._pip_install("v1.2.3", force=True)
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "--upgrade" in cmd
    assert "--force-reinstall" in cmd
    assert cmd[-1] == f"git+{upd.GIT_URL}@v1.2.3"


def test_pip_install_no_force_omits_force_reinstall(monkeypatch):
    """force=False (the default, used by plain --update) has NO --force-reinstall."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._pip_install("v1.2.3")
    assert rc == 0
    assert "--force-reinstall" not in captured["cmd"]


def test_plain_update_path_has_no_force_reinstall(monkeypatch):
    """Regression guard: perform_update() (the --update path) never force-reinstalls."""
    monkeypatch.setattr(upd, "latest_release", lambda: "v9.9.9")
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    assert upd.perform_update() == 0
    assert "--force-reinstall" not in captured["cmd"]


# ---- --update-pr ---------------------------------------------------------- #


def test_pr_ref_format():
    assert upd.pr_ref(2) == "refs/pull/2/head"
    assert upd.pr_ref(42) == "refs/pull/42/head"


def test_perform_update_pr_force_reinstalls_pr_head(monkeypatch, capsys):
    """--update-pr 2: argv has @refs/pull/2/head + --force-reinstall; prints return hint."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update_pr(2)
    out = capsys.readouterr().out
    assert rc == 0

    cmd = captured["cmd"]
    assert "--force-reinstall" in cmd
    assert cmd[-1].endswith("@refs/pull/2/head")
    # Up-front caution about unreviewed code + the return-to-stable hint.
    assert "unreviewed" in out.lower()
    assert "ccusage --update-stable" in out


def test_perform_update_pr_zero_is_rejected_without_pip(monkeypatch, capsys):
    """--update-pr 0: friendly error, NO pip call, non-zero exit."""
    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked for an invalid PR number")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.perform_update_pr(0)
    out = capsys.readouterr().out
    assert rc != 0
    assert "invalid pr number" in out.lower()


def test_perform_update_pr_negative_is_rejected_without_pip(monkeypatch, capsys):
    """--update-pr -1: friendly error, NO pip call, non-zero exit."""
    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked for an invalid PR number")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.perform_update_pr(-1)
    out = capsys.readouterr().out
    assert rc != 0
    assert "invalid pr number" in out.lower()


# ---- --update-prerelease -------------------------------------------------- #


def test_perform_update_prerelease_installs_tag(monkeypatch, capsys):
    """A published prerelease -> force-reinstall that tag."""
    monkeypatch.setattr(upd, "prerelease_release", lambda: "v2.1.0-rc.1")
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update_prerelease()
    out = capsys.readouterr().out
    assert rc == 0
    cmd = captured["cmd"]
    assert "--force-reinstall" in cmd
    assert cmd[-1].endswith("@v2.1.0-rc.1")
    assert "ccusage --update-stable" in out


def test_perform_update_prerelease_falls_back_to_main(monkeypatch):
    """No prerelease -> force-reinstall @main."""
    monkeypatch.setattr(upd, "prerelease_release", lambda: None)
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update_prerelease()
    assert rc == 0
    cmd = captured["cmd"]
    assert "--force-reinstall" in cmd
    assert cmd[-1].endswith("@main")


# ---- --update-stable ------------------------------------------------------ #


def test_perform_update_stable_force_reinstalls_latest(monkeypatch, capsys):
    """A published latest stable -> force-reinstall that tag (the return path)."""
    monkeypatch.setattr(upd, "latest_release", lambda: "v2.0.0")
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update_stable()
    out = capsys.readouterr().out
    assert rc == 0
    cmd = captured["cmd"]
    assert "--force-reinstall" in cmd
    assert cmd[-1].endswith("@v2.0.0")
    assert "official release" in out.lower()


def test_perform_update_stable_no_release_is_friendly(monkeypatch, capsys):
    """No stable release -> friendly message, non-zero, NO pip call."""
    monkeypatch.setattr(upd, "latest_release", lambda: None)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked when there is no stable release")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.perform_update_stable()
    out = capsys.readouterr().out
    assert rc != 0
    assert "no published release" in out.lower()


# ---- prerelease_release() parsing ----------------------------------------- #


def test_prerelease_release_picks_newest_prerelease(monkeypatch):
    """Newest-first list: skip the leading stable, return the first prerelease."""
    payload = [
        {"tag_name": "v2.0.0", "prerelease": False},
        {"tag_name": "v2.1.0-rc.2", "prerelease": True},
        {"tag_name": "v2.1.0-rc.1", "prerelease": True},
    ]
    monkeypatch.setattr(
        upd.urllib.request, "urlopen", lambda *a, **k: _JSONResp(payload)
    )
    assert upd.prerelease_release() == "v2.1.0-rc.2"


def test_prerelease_release_none_when_all_stable(monkeypatch):
    """A list with no prerelease entries -> None."""
    payload = [
        {"tag_name": "v2.0.0", "prerelease": False},
        {"tag_name": "v1.9.0", "prerelease": False},
    ]
    monkeypatch.setattr(
        upd.urllib.request, "urlopen", lambda *a, **k: _JSONResp(payload)
    )
    assert upd.prerelease_release() is None


def test_prerelease_release_none_on_empty(monkeypatch):
    monkeypatch.setattr(
        upd.urllib.request, "urlopen", lambda *a, **k: _JSONResp([])
    )
    assert upd.prerelease_release() is None


def test_prerelease_release_none_on_malformed(monkeypatch):
    """A non-list body (e.g. an error dict) -> None, never a crash."""
    monkeypatch.setattr(
        upd.urllib.request,
        "urlopen",
        lambda *a, **k: _JSONResp({"message": "Not Found"}),
    )
    assert upd.prerelease_release() is None


def test_prerelease_release_none_on_network_error(monkeypatch):
    import urllib.error

    def _raise(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(upd.urllib.request, "urlopen", _raise)
    assert upd.prerelease_release() is None


# ---- --check-prerelease (installs nothing) -------------------------------- #


def test_check_prerelease_installs_nothing(monkeypatch, capsys):
    """--check-prerelease reports installed vs latest prerelease, never calls pip."""
    monkeypatch.setattr(upd, "prerelease_release", lambda: "v2.1.0-rc.1")

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked on --check-prerelease")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.check_prerelease()
    out = capsys.readouterr().out
    assert rc == 0
    assert "v2.1.0-rc.1" in out


def test_check_prerelease_none_is_friendly(monkeypatch, capsys):
    """No prerelease (or GitHub unreachable): friendly message, still installs nothing."""
    monkeypatch.setattr(upd, "prerelease_release", lambda: None)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("pip must not be invoked on --check-prerelease")

    monkeypatch.setattr(upd.subprocess, "run", _boom)

    rc = upd.check_prerelease()
    out = capsys.readouterr().out
    assert rc == 0
    assert "no prerelease" in out.lower()


# ---- pip-missing path (friendly + non-zero) ------------------------------- #


def test_update_pr_pip_missing_is_friendly(monkeypatch, capsys):
    """If pip itself cannot run, --update-pr degrades to a friendly message + non-zero."""
    def _raise(*a, **k):
        raise OSError("no pip")

    monkeypatch.setattr(upd.subprocess, "run", _raise)

    rc = upd.perform_update_pr(2)
    out = capsys.readouterr().out
    assert rc != 0
    assert "could not run pip" in out.lower()
    # The manual fallback line keeps the force flag for the test build.
    assert "--force-reinstall" in out


# --------------------------------------------------------------------------- #
# uv-tool routing: a `uv tool install` has no pip, so the updater must detect
# that and shell out to `uv` instead of failing. Hermetic — `_pip_available()` /
# `_uv_executable()` / `subprocess.run` are all monkeypatched; never touches the
# real interpreter, PATH, network, or pip/uv.
# --------------------------------------------------------------------------- #


def test_should_use_uv_true_when_pip_missing_and_uv_on_path(monkeypatch):
    monkeypatch.setattr(upd, "_pip_available", lambda: False)
    monkeypatch.setattr(upd, "_uv_executable", lambda: "/usr/bin/uv")
    assert upd._should_use_uv() is True


def test_should_use_uv_false_when_pip_present(monkeypatch):
    """Even with uv on PATH, a pip-bearing env (pipx/venv) keeps using pip."""
    monkeypatch.setattr(upd, "_pip_available", lambda: True)
    monkeypatch.setattr(upd, "_uv_executable", lambda: "/usr/bin/uv")
    assert upd._should_use_uv() is False


def test_should_use_uv_false_when_no_uv_executable(monkeypatch):
    """No pip AND no uv on PATH: nothing to route to, so stay on the pip path
    (which will itself degrade to a friendly 'could not run pip' message)."""
    monkeypatch.setattr(upd, "_pip_available", lambda: False)
    monkeypatch.setattr(upd, "_uv_executable", lambda: None)
    assert upd._should_use_uv() is False


def test_uv_install_force_false_still_force_installs_explicit_ref(monkeypatch):
    """Regression test: force=False must NOT run a bare `uv tool upgrade`.

    A bare `uv tool upgrade` silently no-ops (exit 0, "Nothing to upgrade")
    against a tool pinned to a specific rev -- from a prior --update-pr /
    --update-prerelease / --update-stable call, or from a user following the
    README's own "pin a specific release" instructions -- which would make
    `ccusage --update` falsely report success. force=False must therefore
    build the exact same explicit-ref force-install command as force=True.
    """
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._uv_install("v9.9.9", force=False)
    assert rc == 0
    assert captured["cmd"] == ["uv", "tool", "install", "--force", f"git+{upd.GIT_URL}@v9.9.9"]


def test_uv_install_force_runs_uv_tool_install_force_with_ref(monkeypatch):
    """force=True (test-channel / --update-stable) runs `uv tool install --force git+...@<tag>`."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._uv_install("v1.2.3", force=True)
    assert rc == 0
    assert captured["cmd"] == ["uv", "tool", "install", "--force", f"git+{upd.GIT_URL}@v1.2.3"]


def test_uv_install_falls_back_to_main_regardless_of_force(monkeypatch):
    """tag=None (no release / no prerelease published) targets @main, whether
    force is True or False -- they now build an identical command."""
    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._uv_install(None, force=True)
    assert rc == 0
    assert captured["cmd"][-1] == f"git+{upd.GIT_URL}@main"

    rc = upd._uv_install(None, force=False)
    assert rc == 0
    assert captured["cmd"][-1] == f"git+{upd.GIT_URL}@main"


def test_uv_install_missing_uv_is_friendly(monkeypatch, capsys):
    """If uv itself cannot run, _uv_install degrades to a friendly message + non-zero."""
    def _raise(*a, **k):
        raise OSError("no uv")

    monkeypatch.setattr(upd.subprocess, "run", _raise)

    rc = upd._uv_install("v1.2.3", force=True)
    out = capsys.readouterr().out
    assert rc != 0
    assert "could not run uv" in out.lower()
    assert "uv tool install --force" in out


def test_install_dispatches_to_pip_when_should_use_uv_is_false(monkeypatch):
    monkeypatch.setattr(upd, "_should_use_uv", lambda: False)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("_uv_install must not be called on a pip-bearing env")

    monkeypatch.setattr(upd, "_uv_install", _boom)

    captured: dict[str, tuple] = {}

    def _record(tag, force=False):
        captured["args"] = (tag, force)
        return 0

    monkeypatch.setattr(upd, "_pip_install", _record)

    assert upd._install("v1.2.3", force=True) == 0
    assert captured["args"] == ("v1.2.3", True)


def test_install_dispatches_to_uv_when_should_use_uv_is_true(monkeypatch):
    monkeypatch.setattr(upd, "_should_use_uv", lambda: True)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("_pip_install must not be called on a uv-tool env")

    monkeypatch.setattr(upd, "_pip_install", _boom)

    captured: dict[str, tuple] = {}

    def _record(tag, force=False):
        captured["args"] = (tag, force)
        return 0

    monkeypatch.setattr(upd, "_uv_install", _record)

    assert upd._install("v1.2.3", force=True) == 0
    assert captured["args"] == ("v1.2.3", True)


def test_perform_update_routes_through_uv_on_a_uv_tool_install(monkeypatch, capsys):
    """End-to-end: on a pip-less uv-tool env, --update force-installs the
    explicit resolved tag via uv (not a bare `uv tool upgrade`, which would
    no-op against a tool pinned to an older rev)."""
    monkeypatch.setattr(upd, "latest_release", lambda: "v9.9.9")
    monkeypatch.setattr(upd, "_should_use_uv", lambda: True)

    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update()
    out = capsys.readouterr().out
    assert rc == 0
    assert captured["cmd"] == ["uv", "tool", "install", "--force", f"git+{upd.GIT_URL}@v9.9.9"]
    assert "updated successfully" in out.lower()


def test_perform_update_stable_routes_through_uv_on_a_uv_tool_install(monkeypatch, capsys):
    """End-to-end: on a pip-less uv-tool env, --update-stable force-installs via uv."""
    monkeypatch.setattr(upd, "latest_release", lambda: "v2.0.0")
    monkeypatch.setattr(upd, "_should_use_uv", lambda: True)

    captured: dict[str, list[str]] = {}

    def _record(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd.perform_update_stable()
    out = capsys.readouterr().out
    assert rc == 0
    assert captured["cmd"] == ["uv", "tool", "install", "--force", f"git+{upd.GIT_URL}@v2.0.0"]
    assert "official release" in out.lower()


# --------------------------------------------------------------------------- #
# Windows self-replace: upgrading ccusage while ccusage itself drives the
# upgrade means its own launcher .exe is open, so Windows (unlike Unix)
# refuses to overwrite it — pip/uv can still install the new package, but the
# final entry-point step fails with a Win32 sharing violation. Detect that
# specific failure and print a clear hint instead of leaving a bare error on
# screen. Hermetic — `sys.platform` and `subprocess.run` are both
# monkeypatched; never touches a real Windows machine or process.
# --------------------------------------------------------------------------- #

_WIN_LOCK_MSG = (
    "error: Failed to install entrypoint\n"
    "  Caused by: failed to copy file ... ccusage.exe: The process cannot "
    "access the file because it is being used by another process. (os error 32)\n"
)


def test_is_windows_self_replace_error_true_on_windows_with_signature(monkeypatch):
    monkeypatch.setattr(upd.sys, "platform", "win32")
    assert upd._is_windows_self_replace_error(_WIN_LOCK_MSG) is True


def test_is_windows_self_replace_error_false_without_signature(monkeypatch):
    monkeypatch.setattr(upd.sys, "platform", "win32")
    assert upd._is_windows_self_replace_error("some unrelated pip error") is False


def test_is_windows_self_replace_error_false_off_windows(monkeypatch):
    """Even with the exact phrase, only Windows can actually hit this failure."""
    monkeypatch.setattr(upd.sys, "platform", "linux")
    assert upd._is_windows_self_replace_error(_WIN_LOCK_MSG) is False


def test_run_install_prints_hint_on_windows_self_replace_failure(monkeypatch, capsys):
    monkeypatch.setattr(upd.sys, "platform", "win32")

    def _record(cmd, *a, **k):
        return _FakeCompleted(2, stdout=_WIN_LOCK_MSG)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._run_install(["uv", "tool", "upgrade", "cc-usage"], "uv", "uv tool upgrade cc-usage")
    out = capsys.readouterr().out
    assert rc == 2
    assert _WIN_LOCK_MSG in out  # the real tool output is still shown in full
    assert "refusing to replace" in out.lower()
    assert "python -m cc_usage" in out


def test_run_install_no_hint_off_windows_even_with_signature(monkeypatch, capsys):
    """The same failure text on Linux/macOS isn't this bug -- no hint printed."""
    monkeypatch.setattr(upd.sys, "platform", "linux")

    def _record(cmd, *a, **k):
        return _FakeCompleted(1, stdout=_WIN_LOCK_MSG)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._run_install(["pip", "install", "x"], "pip", "pip install x")
    out = capsys.readouterr().out
    assert rc == 1
    assert "refusing to replace" not in out.lower()


def test_run_install_no_hint_on_windows_without_signature(monkeypatch, capsys):
    """A generic Windows failure with no matching signature gets no hint."""
    monkeypatch.setattr(upd.sys, "platform", "win32")

    def _record(cmd, *a, **k):
        return _FakeCompleted(1, stdout="some unrelated pip error\n")

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._run_install(["pip", "install", "x"], "pip", "pip install x")
    out = capsys.readouterr().out
    assert rc == 1
    assert "refusing to replace" not in out.lower()


def test_run_install_no_hint_on_success_even_with_signature(monkeypatch, capsys):
    """returncode == 0: never print the hint, no matter what text is present."""
    monkeypatch.setattr(upd.sys, "platform", "win32")

    def _record(cmd, *a, **k):
        return _FakeCompleted(0, stdout=_WIN_LOCK_MSG)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc = upd._run_install(["uv", "tool", "upgrade", "cc-usage"], "uv", "uv tool upgrade cc-usage")
    out = capsys.readouterr().out
    assert rc == 0
    assert "refusing to replace" not in out.lower()


def test_pip_install_and_uv_install_both_surface_the_hint(monkeypatch, capsys):
    """Both backends route through _run_install(), so both surface the hint."""
    monkeypatch.setattr(upd.sys, "platform", "win32")

    def _record(cmd, *a, **k):
        return _FakeCompleted(2, stdout=_WIN_LOCK_MSG)

    monkeypatch.setattr(upd.subprocess, "run", _record)

    rc_pip = upd._pip_install("v1.2.3", force=True)
    assert rc_pip == 2
    assert "refusing to replace" in capsys.readouterr().out.lower()

    rc_uv = upd._uv_install("v1.2.3", force=True)
    assert rc_uv == 2
    assert "refusing to replace" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- #
# Argparse: the four flags parse to the right attributes with no ambiguity.
# --------------------------------------------------------------------------- #


def test_argparse_parses_test_channel_flags():
    from cc_usage.cli import build_parser

    p = build_parser()

    a = p.parse_args(["--update-pr", "2"])
    assert a.update_pr == 2
    assert a.update_prerelease is False
    assert a.update_stable is False
    assert a.check_prerelease is False
    # --update-pr must not also flip the plain --update flag (no abbreviation bleed).
    assert a.update is False

    a = p.parse_args(["--update-prerelease"])
    assert a.update_prerelease is True
    assert a.update_pr is None

    a = p.parse_args(["--update-stable"])
    assert a.update_stable is True

    a = p.parse_args(["--check-prerelease"])
    assert a.check_prerelease is True

    # Plain --update stays distinct from all the --update-* variants.
    a = p.parse_args(["--update"])
    assert a.update is True
    assert a.update_pr is None
    assert a.update_prerelease is False
    assert a.update_stable is False
