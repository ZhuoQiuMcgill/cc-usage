"""Direct Claude and Codex provider-limit fetching."""

import io
import json

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
