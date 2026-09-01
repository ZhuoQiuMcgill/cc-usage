"""Direct Claude and Codex provider-limit fetching."""

import io
import json
import os
import sys
import textwrap

import pytest

import cc_usage.limits_fetch as limits_fetch
from cc_usage.limits_fetch import (
    LimitFetchError,
    fetch_claude_limits,
    fetch_provider_limits,
    load_limits_cache,
    normalize_claude_limits,
    normalize_codex_limits,
    save_limits_cache,
)
from cc_usage.ratelimits import account_buckets


CLAUDE_RESPONSE = {
    "limits": [
        {
            "kind": "session",
            "percent": 26,
            "resets_at": "2026-07-13T00:50:00+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_all",
            "percent": 69,
            "resets_at": "2026-07-13T10:00:00+00:00",
            "scope": None,
        },
        {
            "kind": "weekly_scoped",
            "percent": 99,
            "resets_at": "2026-07-13T10:00:00+00:00",
            "scope": {"model": {"display_name": "Fable"}},
        },
    ]
}

CODEX_RESPONSE = {
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "limitName": None,
            "primary": {
                "usedPercent": 35,
                "windowDurationMins": 10080,
                "resetsAt": 2_000_000_000,
            },
            "secondary": None,
        },
        "codex_spark": {
            "limitId": "codex_spark",
            "limitName": "GPT Spark",
            "primary": {
                "usedPercent": 4,
                "windowDurationMins": 300,
                "resetsAt": 2_000_000_100,
            },
            "secondary": None,
        },
    }
}


def test_normalize_both_providers_keeps_all_scoped_limits():
    claude = normalize_claude_limits(CLAUDE_RESPONSE, now=10)
    codex = normalize_codex_limits(CODEX_RESPONSE, now=20)
    buckets = account_buckets(
        {"claude:personal": claude, "codex:codex": codex},
        ["personal"],
        ["codex"],
        multi_claude=False,
        multi_codex=False,
    )

    assert [bucket.label for bucket in buckets] == [
        "CLAUDE 5-HOUR",
        "CLAUDE WEEKLY",
        "CLAUDE FABLE WEEKLY",
        "CODEX WEEKLY",
        "CODEX GPT SPARK 5-HOUR",
    ]
    assert [bucket.used_percentage for bucket in buckets] == [26, 69, 99, 35, 4]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_claude_fetch_uses_oauth_in_memory(tmp_path):
    credentials = tmp_path / ".credentials.json"
    credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "secret-test-token"}}),
        "utf-8",
    )
    observed = {}

    def opener(request, timeout):
        observed["authorization"] = request.headers["Authorization"]
        observed["beta"] = request.headers["Anthropic-beta"]
        observed["timeout"] = timeout
        return _Response(json.dumps(CLAUDE_RESPONSE).encode())

    capture = fetch_claude_limits(credentials, timeout=7, opener=opener)

    assert observed == {
        "authorization": "Bearer secret-test-token",
        "beta": "oauth-2025-04-20",
        "timeout": 7,
    }
    assert capture["source"] == "claude"
    assert "secret-test-token" not in json.dumps(capture)


def test_provider_cache_contains_results_not_credentials(tmp_path):
    path = tmp_path / "provider-limits.json"
    captures = {
        "claude:personal": normalize_claude_limits(CLAUDE_RESPONSE, now=10),
        "codex:codex": normalize_codex_limits(CODEX_RESPONSE, now=20),
    }
    save_limits_cache(captures, path)
    assert load_limits_cache(path) == captures
    assert "Authorization" not in path.read_text("utf-8")


def test_fetch_failure_retains_last_good_provider(monkeypatch):
    old = {"claude": normalize_claude_limits(CLAUDE_RESPONSE, now=10)}

    def fail():
        raise LimitFetchError("Claude temporarily unavailable")

    monkeypatch.setattr(limits_fetch, "fetch_claude_limits", fail)
    monkeypatch.setattr(
        limits_fetch,
        "fetch_codex_limits",
        lambda: normalize_codex_limits(CODEX_RESPONSE, now=20),
    )
    captures, warnings = fetch_provider_limits(old)

    assert captures["claude"] is old["claude"]
    assert captures["codex"]["source"] == "codex"
    assert warnings == ["Claude temporarily unavailable"]


@pytest.mark.parametrize("payload", [{}, {"limits": []}, None])
def test_invalid_claude_payload_is_rejected(payload):
    with pytest.raises(LimitFetchError):
        normalize_claude_limits(payload)

def test_expired_claude_token_is_refreshed_before_fetch(tmp_path, monkeypatch):
    credentials = tmp_path / ".credentials.json"
    credentials.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "expired-token", "expiresAt": 1}}
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        limits_fetch,
        "_refresh_claude_credentials",
        lambda path, timeout, config_dir=None: "fresh-token",
    )

    def opener(request, timeout):
        assert request.headers["Authorization"] == "Bearer fresh-token"
        return _Response(json.dumps(CLAUDE_RESPONSE).encode())

    assert fetch_claude_limits(credentials, opener=opener)["source"] == "claude"


def test_claude_refresh_delegates_to_official_client(tmp_path, monkeypatch):
    credentials = tmp_path / ".credentials.json"
    credentials.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "expired-token", "expiresAt": 1}}
        ),
        "utf-8",
    )
    observed = {}
    monkeypatch.setattr(limits_fetch.shutil, "which", lambda name: "claude")

    def run(args, **kwargs):
        observed["args"] = args
        credentials.write_text(
            json.dumps(
                {"claudeAiOauth": {"accessToken": "fresh-token", "expiresAt": 9e15}}
            ),
            "utf-8",
        )

    monkeypatch.setattr(limits_fetch.subprocess, "run", run)
    token = limits_fetch._refresh_claude_credentials(credentials, 5)

    assert token == "fresh-token"
    assert observed["args"] == ["claude", "--print", "--max-turns", "0", ""]


# ── T14: a failing Codex app-server must never escape as a raw OSError ──────────
def _stub_codex(tmp_path, body: str):
    """Write an executable stub standing in for the `codex` CLI and return its path.

    The RPC is driven for real against this process: the crash under test lives in the
    subprocess plumbing itself (a write into a pipe whose read end is gone), so stubbing
    `_run_codex_rpc` — the function under test — would prove nothing.
    """
    script = tmp_path / "codex"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip(), "utf-8")
    script.chmod(0o755)
    return script


posix_stub = pytest.mark.skipif(
    os.name == "nt", reason="the stub CLI is a POSIX shebang script"
)


@posix_stub
def test_codex_rpc_broken_pipe_surfaces_as_limit_fetch_error(tmp_path, monkeypatch):
    """The reported v2.4.2 crash. The installed codex CLI exits immediately (status 2);
    when our `initialize` write loses the race to the closing read end, `stdin.write`
    raises BrokenPipeError — an OSError, not a LimitFetchError, so it escaped the module
    and (via Textual's exit_on_error worker) killed the whole panel.

    The ordering is *forced* — wait for the child to die before the first write — because
    the bug is intermittent by nature and must not be left to timing.
    """
    stub = _stub_codex(tmp_path, "import sys\nsys.exit(2)\n")
    monkeypatch.setattr(limits_fetch, "_codex_executable", lambda: str(stub))
    real_popen = limits_fetch.subprocess.Popen

    def popen_then_reap(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        process.wait(timeout=10)  # the child is gone BEFORE we write -> guaranteed EPIPE
        return process

    monkeypatch.setattr(limits_fetch.subprocess, "Popen", popen_then_reap)

    with pytest.raises(LimitFetchError) as excinfo:
        limits_fetch._run_codex_rpc(timeout=5)

    assert "status 2" in str(excinfo.value)  # actionable: names the child's exit status
    assert isinstance(excinfo.value, limits_fetch.CodexAppServerUnavailable)


@posix_stub
def test_codex_rpc_survivable_ordering_still_raises_limit_fetch_error(
    tmp_path, monkeypatch
):
    """The other side of the same race: the write lands in the pipe buffer before the
    child hangs up. That ordering was already survivable — pinned here so the fix for the
    losing ordering cannot change what the caller sees on the winning one."""
    stub = _stub_codex(tmp_path, "import sys\nsys.stdin.readline()\nsys.exit(2)\n")
    monkeypatch.setattr(limits_fetch, "_codex_executable", lambda: str(stub))

    with pytest.raises(LimitFetchError) as excinfo:
        limits_fetch._run_codex_rpc(timeout=5)

    assert "closed before returning rate limits" in str(excinfo.value)


@posix_stub
def test_codex_rpc_write_failure_mid_conversation_is_normalized(tmp_path, monkeypatch):
    """A child that answers `initialize` and then hangs up on stdin while still running:
    the follow-up write fails with the process alive, so no exit status can be quoted. It
    must still be a LimitFetchError — and a retryable one, since the app-server did talk."""
    stub = _stub_codex(
        tmp_path,
        r"""
        import os, sys, time
        sys.stdin.readline()                      # take the initialize frame
        os.close(0)                               # hang up on stdin, stay alive
        sys.stdout.write('{"id":1,"result":{}}\n')
        sys.stdout.flush()
        time.sleep(30)
        """,
    )
    monkeypatch.setattr(limits_fetch, "_codex_executable", lambda: str(stub))

    with pytest.raises(LimitFetchError) as excinfo:
        limits_fetch._run_codex_rpc(timeout=5)

    assert "connection failed" in str(excinfo.value)
    assert not isinstance(excinfo.value, limits_fetch.CodexAppServerUnavailable)
