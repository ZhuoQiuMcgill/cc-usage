"""Credential-safe provider usage-limit fetchers.

Codex delegates authentication and token refresh to the documented app-server RPC.
Claude has no account RPC, so its locally stored OAuth access token is read in memory
for the same read-only usage endpoint used by Claude Code. Tokens are never logged or
persisted by ccusage; only normalized percentages and reset timestamps are cached.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .paths import CLAUDE_CREDENTIALS, LIMITS_CACHE_JSON
from .ratelimits import label_for_minutes

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_OAUTH_BETA = "oauth-2025-04-20"


class LimitFetchError(RuntimeError):
    """A provider could not return current limits."""


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def captured_at(capture: object) -> float:
    """The capture's `captured_at`, or -inf when absent — a total order for
    freshest-wins precedence between an RPC capture and a transcript snapshot (T13)."""
    value = capture.get("captured_at") if isinstance(capture, dict) else None
    return float(value) if isinstance(value, (int, float)) else float("-inf")


def _capture(provider: str, buckets: dict[str, dict], now: float | None = None) -> dict:
    return {
        "captured_at": float(time.time() if now is None else now),
        "source": provider,
        "rate_limits": buckets,
    }


def normalize_claude_limits(data: object, now: float | None = None) -> dict:
    """Normalize Claude's current usage response, including scoped model limits."""
    if not isinstance(data, dict):
        raise LimitFetchError("Claude returned an invalid usage response")
    buckets: dict[str, dict] = {}
    limits = data.get("limits")
    if isinstance(limits, list):
        for index, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            percent = item.get("percent")
            resets_at = _epoch(item.get("resets_at"))
            if not isinstance(percent, (int, float)) or resets_at is None:
                continue
            kind = str(item.get("kind") or f"limit_{index}")
            scope = item.get("scope")
            scope_name = None
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict):
                    scope_name = model.get("display_name") or model.get("id")
                scope_name = scope_name or scope.get("surface")
            if kind == "session":
                label = "5-HOUR"
            elif kind == "weekly_all":
                label = "WEEKLY"
            elif kind == "weekly_scoped":
                label = f"{scope_name or 'SCOPED'} WEEKLY".upper()
            else:
                label = kind.replace("_", " ").upper()
            key = kind if kind not in buckets else f"{kind}_{index}"
            buckets[key] = {
                "label": label,
                "used_percentage": float(percent),
                "resets_at": resets_at,
            }
    if not buckets:
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            percent = item.get("utilization")
            resets_at = _epoch(item.get("resets_at"))
            if isinstance(percent, (int, float)) and resets_at is not None:
                buckets[str(key)] = {
                    "used_percentage": float(percent),
                    "resets_at": resets_at,
                }
    if not buckets:
        raise LimitFetchError("Claude returned no usable usage limits")
    return _capture("claude", buckets, now)


def _read_claude_oauth(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LimitFetchError("Claude OAuth credentials are unavailable") from exc
    if not isinstance(oauth, dict):
        raise LimitFetchError("Claude OAuth credentials are unavailable")
    return oauth


def _read_claude_access_token(path: Path) -> str:
    token = _read_claude_oauth(path).get("accessToken")
    if not isinstance(token, str) or not token:
        raise LimitFetchError("Claude OAuth access token is unavailable")
    return token


def _claude_token_expired(path: Path) -> bool:
    expires_at = _read_claude_oauth(path).get("expiresAt")
    return isinstance(expires_at, (int, float)) and expires_at <= time.time() * 1000 + 30_000


def _refresh_claude_credentials(
    path: Path, timeout: float, config_dir: Path | None = None
) -> str:
    executable = shutil.which("claude.exe" if os.name == "nt" else "claude")
    if executable is None and os.name == "nt":
        executable = shutil.which("claude")
    if executable is None:
        raise LimitFetchError("Claude credentials expired and the Claude executable was not found")
    before = _read_claude_access_token(path)
    # For a non-default account (T11) point the official client at that config dir so
    # it refreshes the right account's token; the default account inherits the env.
    env = None
    if config_dir is not None:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    try:
        subprocess.run(
            [executable, "--print", "--max-turns", "0", ""],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LimitFetchError(f"Claude credential refresh failed: {exc}") from exc
    after = _read_claude_access_token(path)
    if after == before and _claude_token_expired(path):
        raise LimitFetchError("Claude credentials remain expired; run Claude Code to sign in")
    return after

def fetch_claude_limits(
    credentials_path: Path = CLAUDE_CREDENTIALS,
    *,
    config_dir: Path | None = None,
    timeout: float = 15,
    opener: Callable = urllib.request.urlopen,
) -> dict:
    path = Path(credentials_path)
    token = (
        _refresh_claude_credentials(path, timeout, config_dir)
        if _claude_token_expired(path)
        else _read_claude_access_token(path)
    )

    def request_usage(access_token: str) -> object:
        request = urllib.request.Request(
            _CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "anthropic-beta": _CLAUDE_OAUTH_BETA,
                "User-Agent": f"cc-usage/{__version__}",
            },
        )
        with opener(request, timeout=timeout) as response:
            return json.load(response)

    try:
        data = request_usage(token)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise LimitFetchError(f"Claude usage fetch failed: HTTP {exc.code}") from exc
        try:
            data = request_usage(_refresh_claude_credentials(path, timeout, config_dir))
        except (OSError, ValueError, urllib.error.URLError) as retry_exc:
            raise LimitFetchError(f"Claude usage fetch failed: {retry_exc}") from retry_exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise LimitFetchError(f"Claude usage fetch failed: {exc}") from exc
    return normalize_claude_limits(data)

def _codex_executable() -> str:
    names = ("codex.cmd", "codex") if os.name == "nt" else ("codex",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise LimitFetchError("Codex executable was not found")


def _run_codex_rpc(timeout: float = 20) -> dict:
    try:
        process = subprocess.Popen(
            [_codex_executable(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise LimitFetchError(f"Codex app-server could not start: {exc}") from exc
    responses: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(message: dict) -> None:
        if process.stdin is None:
            raise LimitFetchError("Codex app-server stdin is unavailable")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    deadline = time.monotonic() + timeout
    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "cc_usage",
                        "title": "CC Usage",
                        "version": __version__,
                    }
                },
            }
        )
        initialized = False
        while time.monotonic() < deadline:
            try:
                line = responses.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise LimitFetchError("Codex rate-limit fetch timed out") from exc
            if line is None:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == 1 and not initialized:
                send({"method": "initialized", "params": {}})
                send({"method": "account/rateLimits/read", "id": 2, "params": {}})
                initialized = True
            elif message.get("id") == 2:
                error = message.get("error")
                if error:
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise LimitFetchError(f"Codex rate-limit fetch failed: {detail}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise LimitFetchError("Codex returned an invalid rate-limit response")
                return result
        raise LimitFetchError("Codex app-server closed before returning rate limits")
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            process.kill()


def normalize_codex_limits(data: object, now: float | None = None) -> dict:
    if not isinstance(data, dict):
        raise LimitFetchError("Codex returned an invalid rate-limit response")
    by_id = data.get("rateLimitsByLimitId")
    if not isinstance(by_id, dict) or not by_id:
        single = data.get("rateLimits")
        by_id = {"codex": single} if isinstance(single, dict) else {}
    buckets: dict[str, dict] = {}
    for limit_id, limit in by_id.items():
        if not isinstance(limit, dict):
            continue
        limit_name = limit.get("limitName")
        for slot in ("primary", "secondary", "individualLimit"):
            window = limit.get(slot)
            if not isinstance(window, dict):
                continue
            percent = window.get("usedPercent")
            resets_at = _epoch(window.get("resetsAt"))
            minutes = window.get("windowDurationMins")
            if not isinstance(percent, (int, float)) or resets_at is None:
                continue
            duration = label_for_minutes(minutes, slot)
            label = f"{limit_name} {duration}" if limit_name else duration
            buckets[f"{limit_id}_{slot}"] = {
                "label": label.upper(),
                "used_percentage": float(percent),
                "resets_at": resets_at,
                "window_minutes": float(minutes) if isinstance(minutes, (int, float)) else None,
            }
    if not buckets:
        raise LimitFetchError("Codex returned no usable rate limits")
    return _capture("codex", buckets, now)


def fetch_codex_limits(*, timeout: float = 20) -> dict:
    return normalize_codex_limits(_run_codex_rpc(timeout))


def load_limits_cache(path: Path = LIMITS_CACHE_JSON) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        providers = data.get("providers") if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(providers, dict):
        return {}
    # Accept the per-account keys only (`claude:<label>` from T11, `codex:<label>` from
    # T13). A bare legacy `claude` or `codex` key from a pre-multi-account cache is
    # pruned here — the per-account renderer can't attribute it to a root, so keeping it
    # would make the panel claim usable provider data it cannot render, and it would be
    # re-persisted as cruft on every save (mirrors the T11 bare-`claude` pruning).
    return {
        name: capture
        for name, capture in providers.items()
        if isinstance(capture, dict)
        and (name.startswith("codex:") or name.startswith("claude:"))
    }


def save_limits_cache(captures: dict[str, dict], path: Path = LIMITS_CACHE_JSON) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"providers": captures}, indent=2) + "\n",
            "utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        return


def fetch_provider_limits(
    existing: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], list[str]]:
    captures = dict(existing or {})
    warnings: list[str] = []
    for provider, fetcher in (
        ("claude", fetch_claude_limits),
        ("codex", fetch_codex_limits),
    ):
        try:
            captures[provider] = fetcher()
        except LimitFetchError as exc:
            warnings.append(str(exc))
    return captures, warnings


def fetch_account_limits(
    roots, existing: dict[str, dict] | None = None
) -> tuple[dict[str, dict], list[str]]:
    """Fetch each enabled Claude account's limits (T11 R5).

    `roots` are the enabled Claude `Root`s. Each account is fetched from its own
    `<root>/.credentials.json` — the access token held only in memory, the default
    account inheriting the env and any other pointed at its config dir for a
    credential refresh. Accounts are isolated: one account's failure keeps its
    last-good capture (carried in `existing`) and appends a warning without
    blocking the others. Returns captures keyed `claude:<label>` per account and the
    collected warnings. Reuses the single-provider fetcher — no forked fetch path.

    Codex limits are assembled separately by the engine from rollout snapshots
    (T13) — off both the network and render paths — so they are not fetched here.
    """
    captures = dict(existing or {})
    warnings: list[str] = []
    for root in roots:
        try:
            captures[f"claude:{root.label}"] = fetch_claude_limits(
                root.path / ".credentials.json",
                config_dir=None if getattr(root, "source", None) == "auto" else root.path,
            )
        except LimitFetchError as exc:
            warnings.append(f"{root.label}: {exc}")
    return captures, warnings
