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
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


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
