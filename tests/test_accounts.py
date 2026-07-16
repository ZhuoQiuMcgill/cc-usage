"""Multi-account support (T11): root discovery, account tagging, scope filtering,
the by-account rollup (incl. the single-account zero-noise regression pin), per-account
limits, and root enable/disable. Hermetic — synthetic tmp roots and stubbed fetch seams;
no real ~/.claude read and no network."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

import cc_usage.engine as engine_module
import cc_usage.limits_fetch as limits_fetch
from cc_usage.accounts import (
    CLAUDE_PROVIDER,
    CODEX_ACCOUNT,
    CODEX_PROVIDER,
    Root,
    discover_claude_roots,
    discover_codex_roots,
)
from cc_usage.aggregate import aggregate_accounts
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.limits_fetch import (
    LimitFetchError,
    fetch_account_limits,
    load_limits_cache,
    normalize_claude_limits,
    save_limits_cache,
)
from cc_usage.parser import Parser, ScanCancelled, UsageRecord
from cc_usage.ratelimits import account_buckets
from cc_usage.render import (
    RenderState,
    account_scope_line,
    build_panel,
    by_account_block,
    limits_block,
)
from cc_usage.themes import get_theme

PRICING = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}
THEME = get_theme("dark")
NOW = 1_000_000_000.0


def _plain(renderable, width: int = 90) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width, no_color=True).print(renderable)
    return buf.getvalue()


def _mkroot(base: Path, name: str) -> Path:
    """Create a config-dir root with a projects tree and return the config dir."""
    root = base / name
    (root / "projects").mkdir(parents=True)
    return root


def _claude_line(req: str, mid: str, inp: int, out: int = 0) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "requestId": req,
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {
                    "id": mid,
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": inp, "output_tokens": out},
                },
            }
        )
        + "\n"
    )


def _mkcodex(base: Path, name: str) -> Path:
    """Create a Codex config-dir root with a sessions tree and return the config dir."""
    root = base / name
    (root / "sessions").mkdir(parents=True)
    return root


def _codex_lines(inp: int = 100, out: int = 10, ts: str = "2026-07-12T12:00:00Z", model: str = "gpt-test") -> str:
    """A minimal Codex rollout: a turn_context (model) + one token_count event."""
    ctx = json.dumps({"timestamp": ts, "type": "turn_context", "payload": {"model": model}}) + "\n"
    tok = (
        json.dumps(
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": 0,
                            "output_tokens": out,
                            "total_tokens": inp + out,
                        },
                        "last_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": 0,
                            "output_tokens": out,
                            "total_tokens": inp + out,
                        },
                    },
                },
            }
        )
        + "\n"
    )
    return ctx + tok


# ── R1: root discovery ──────────────────────────────────────────────────────────
def test_discovery_default_only(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    roots = discover_claude_roots(Config(), home=home, environ={})
    assert [(r.label, r.source, r.enabled) for r in roots] == [("personal", "auto", True)]


def test_discovery_env_root_labeled_and_ordered(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    company = _mkroot(home, ".claude-rdqcc")  # the motivating CLAUDE_CONFIG_DIR case
    roots = discover_claude_roots(
        Config(), home=home, environ={"CLAUDE_CONFIG_DIR": str(company)}
    )
    assert [r.label for r in roots] == ["personal", "rdqcc"]
    assert [r.source for r in roots] == ["auto", "env"]
    assert roots[1].projects == company / "projects"


def test_discovery_env_equal_to_default_is_deduped(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    roots = discover_claude_roots(
        Config(), home=home, environ={"CLAUDE_CONFIG_DIR": str(home / ".claude")}
    )
    assert len(roots) == 1 and roots[0].source == "auto"


def test_discovery_config_roots_and_missing_skipped(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    extra = _mkroot(tmp_path, "extra")
    cfg = Config(
        claude_roots=[
            {"path": str(extra), "label": "work"},
            {"path": str(tmp_path / "ghost")},  # missing dir -> skipped silently
        ]
    )
    roots = discover_claude_roots(cfg, home=home, environ={})
    assert [r.label for r in roots] == ["personal", "work"]


def test_discovery_label_derivation_and_collision_suffix(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    a = _mkroot(tmp_path / "a", ".claude-team")
    b = _mkroot(tmp_path / "b", ".claude-team")  # same derived label -> suffix
    cfg = Config(claude_roots=[{"path": str(a)}, {"path": str(b)}])
    roots = discover_claude_roots(cfg, home=home, environ={})
    assert [r.label for r in roots] == ["personal", "team", "team-2"]


def test_discovery_codex_label_is_reserved(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    c = _mkroot(tmp_path / "c", ".claude-codex")  # would derive "codex" (reserved)
    cfg = Config(claude_roots=[{"path": str(c)}])
    roots = discover_claude_roots(cfg, home=home, environ={})
    assert CODEX_ACCOUNT not in [r.label for r in roots]
    assert roots[-1].label == "codex-2"


def test_discovery_all_label_is_reserved(tmp_path):
    """`all` is the scope sentinel: a root labelled "all" could never be isolated
    (cycling to it would read as the all-accounts scope), so it must be suffixed."""
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    a = _mkroot(tmp_path / "x", ".claude-all")  # would derive "all"
    b = _mkroot(tmp_path / "y", "work")
    cfg = Config(claude_roots=[{"path": str(a)}, {"path": str(b), "label": "all"}])
    roots = discover_claude_roots(cfg, home=home, environ={})
    labels = [r.label for r in roots]
    assert "all" not in labels
    assert labels == ["personal", "all-2", "all-3"]


def test_discovery_enabled_false_and_disabled_roots(tmp_path):
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    e = _mkroot(tmp_path, "e")
    f = _mkroot(tmp_path, "f")
    cfg = Config(
        claude_roots=[
            {"path": str(e), "label": "e", "enabled": False},  # config hard-disable
            {"path": str(f), "label": "f"},
        ],
        disabled_roots=[str(f)],  # UI toggle-off
    )
    by = {r.label: r for r in discover_claude_roots(cfg, home=home, environ={})}
    assert by["personal"].enabled is True
    assert by["e"].enabled is False
    assert by["f"].enabled is False


# ── R2: account tagging + cache round-trip ──────────────────────────────────────
def test_records_tagged_by_root(tmp_path):
    r1 = _mkroot(tmp_path, "personal")
    r2 = _mkroot(tmp_path, "company")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(_claude_line("b", "b", 200))
    parser = Parser(
        PRICING, roots=[(r1 / "projects", "personal"), (r2 / "projects", "company")]
    )
    parser.scan()
    assert {r.account: r.input_tokens for r in parser.records} == {
        "personal": 100,
        "company": 200,
    }


def test_cache_round_trips_account_labels(tmp_path):
    r1 = _mkroot(tmp_path, "personal")
    r2 = _mkroot(tmp_path, "company")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(_claude_line("b", "b", 200))
    cache = tmp_path / "cache.pkl"
    both = [(r1 / "projects", "personal"), (r2 / "projects", "company")]

    writer = Parser(PRICING, cache_path=cache, roots=both)
    writer.scan()
    writer.save_cache()

    warm = Parser(PRICING, cache_path=cache, roots=both)
    warm.scan()
    assert warm.stats.lines_read == 0  # warm start read nothing new
    assert {r.account for r in warm.records} == {"personal", "company"}


def test_root_set_change_invalidates_cache_and_rescans(tmp_path):
    """Disabling a root (a changed root set) rebuilds the cache and drops that root's
    records; re-enabling restores them (R2 fingerprint + R7 data behaviour)."""
    r1 = _mkroot(tmp_path, "personal")
    r2 = _mkroot(tmp_path, "company")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(_claude_line("b", "b", 200))
    cache = tmp_path / "cache.pkl"
    both = [(r1 / "projects", "personal"), (r2 / "projects", "company")]
    one = [(r1 / "projects", "personal")]

    first = Parser(PRICING, cache_path=cache, roots=both)
    first.scan()
    first.save_cache()
    assert {r.account for r in first.records} == {"personal", "company"}

    # "disable company": same cache, fewer roots -> fingerprint mismatch -> full rebuild.
    disabled = Parser(PRICING, cache_path=cache, roots=one)
    disabled.scan()
    disabled.save_cache()
    assert {r.account for r in disabled.records} == {"personal"}
    assert disabled.stats.lines_read == 1  # rescanned, not served from stale cache

    # "re-enable company": company's records come back.
    restored = Parser(PRICING, cache_path=cache, roots=both)
    restored.scan()
    assert {r.account for r in restored.records} == {"personal", "company"}


def test_label_rename_only_invalidates_cache(tmp_path):
    """Same paths, changed label: cached records carry the OLD label, so the roots
    fingerprint must treat a rename as a changed root set and rebuild (labels are
    part of _roots_fingerprint, not just the path set)."""
    r1 = _mkroot(tmp_path, "personal")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    cache = tmp_path / "cache.pkl"

    writer = Parser(PRICING, cache_path=cache, roots=[(r1 / "projects", "personal")])
    writer.scan()
    writer.save_cache()

    renamed = Parser(PRICING, cache_path=cache, roots=[(r1 / "projects", "corp")])
    renamed.scan()
    assert renamed.stats.lines_read == 1  # full re-read, not served warm
    assert {r.account for r in renamed.records} == {"corp"}  # new label applied


# ── R3/R4 engine helpers ────────────────────────────────────────────────────────
def _rec(
    account: str,
    age: float = 100.0,
    *,
    cost: float,
    inp: int,
    model="claude-opus-4-8",
    provider: str | None = None,
):
    # Codex records default to the Codex provider (the synthetic `codex` account),
    # so existing single-codex tests keep working; multi-codex tests pass it in.
    if provider is None:
        provider = CODEX_PROVIDER if account == CODEX_ACCOUNT else CLAUDE_PROVIDER
    return UsageRecord(
        ts=NOW - age,
        model_raw=model,
        model_norm=model,
        known=True,
        input_tokens=inp,
        output_tokens=0,
        cache_read=0,
        cache_creation=0,
        cost=cost,
        account=account,
        provider=provider,
    )


def _codex_roots_for(records, codex_labels):
    """Synthetic codex Root objects for an engine under test — either the given
    labels or, when None, the distinct codex accounts present in `records`."""
    if codex_labels is None:
        codex_labels = []
        for r in records:
            if r.provider == CODEX_PROVIDER and r.account not in codex_labels:
                codex_labels.append(r.account)
    return [
        Root(lbl, Path(f"/c/{lbl}"), Path(f"/c/{lbl}/sessions"), "auto" if i == 0 else "config")
        for i, lbl in enumerate(codex_labels)
    ]


def _engine_with(records, labels, codex_labels=None):
    eng = Engine(Config(), cache_path=None)
    eng.roots = [
        Root(label, Path(f"/x/{label}"), Path(f"/x/{label}/projects"), "auto" if i == 0 else "config")
        for i, label in enumerate(labels)
    ]
    eng.codex_roots = _codex_roots_for(records, codex_labels)
    eng.parser.records = records
    eng._scanned = True
    eng._refresh_account_flags()
    return eng


# ── R3: account scope filters every view ────────────────────────────────────────
def test_scope_filters_windows_models_heartbeat_and_range():
    records = [
        _rec("personal", cost=2.0, inp=100),
        _rec("rdqcc", cost=3.0, inp=200, model="claude-sonnet-4-6"),
        _rec(CODEX_ACCOUNT, cost=1.0, inp=50),
    ]
    eng = _engine_with(records, ["personal", "rdqcc"])

    eng.account_scope = "all"
    s = eng.snapshot(NOW)
    assert s.windows["all"].input_tokens == 350  # everything, incl. codex
    assert eng.range_metrics(NOW - 200, NOW + 200).input_tokens == 350

    eng.account_scope = "rdqcc"  # a specific account excludes codex and the other account
    s = eng.snapshot(NOW)
    assert s.windows["all"].input_tokens == 200
    assert set(s.windows["all"].models) == {"claude-sonnet-4-6"}
    assert s.heartbeat.record_count == 1
    assert eng.range_metrics(NOW - 200, NOW + 200).input_tokens == 200


# ── R4: by-account rollup + zero-noise pin ──────────────────────────────────────
def test_by_account_block_two_accounts_tokens_cost_share():
    records = [
        _rec("personal", cost=2.0, inp=100),
        _rec("rdqcc", cost=6.0, inp=300),
    ]
    eng = _engine_with(records, ["personal", "rdqcc"])
    s = eng.snapshot(NOW)
    assert [a.label for a in s.accounts] == ["rdqcc", "personal"]  # cost desc

    out = _plain(by_account_block(s, THEME))
    assert "By account" in out
    assert "rdqcc" in out and "personal" in out
    assert "75%" in out and "25%" in out  # 6/8 and 2/8 share-of-cost


def test_codex_row_only_when_codex_data_in_window():
    # One Claude account + Codex data -> a two-row block including a Codex row.
    eng = _engine_with(
        [_rec("personal", cost=2.0, inp=100), _rec(CODEX_ACCOUNT, cost=1.0, inp=50)],
        ["personal"],
    )
    s = eng.snapshot(NOW)
    assert [a.label for a in s.accounts] == ["personal", CODEX_ACCOUNT]
    assert _plain(by_account_block(s, THEME)).count("\n") >= 3
    assert "Codex" in _plain(by_account_block(s, THEME))


def test_codex_row_absent_when_no_codex_in_window():
    # Two Claude accounts, no Codex -> block shows the two accounts, no Codex row.
    eng = _engine_with(
        [_rec("personal", cost=2.0, inp=100), _rec("rdqcc", cost=1.0, inp=50)],
        ["personal", "rdqcc"],
    )
    s = eng.snapshot(NOW)
    assert [a.label for a in s.accounts] == ["personal", "rdqcc"]
    assert "Codex" not in _plain(by_account_block(s, THEME))


def test_single_account_no_codex_is_zero_noise():
    """Exactly one Claude root and no Codex: no by-account block, no scope line, and
    the whole panel is byte-identical to the pre-multi-account render (regression pin)."""
    eng = _engine_with([_rec("personal", cost=2.0, inp=100)], ["personal"])
    s = eng.snapshot(NOW)
    assert s.accounts == []
    assert s.account_ui is False
    assert account_scope_line(s, THEME) is None
    assert by_account_block(s, THEME) is None

    # Full-panel byte pin against the fixture captured from the pre-T11 build_panel.
    # The clock strings in the panel are local-time, so pin TZ=UTC for the render and
    # restore it afterwards (never leak process-global TZ state into other tests).
    import os
    import time

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        from cc_usage.aggregate import ModelAgg, WindowAgg, series

        now = 1_700_000_000.0
        windows = {}
        for key in ("1h", "5h", "24h", "7d", "all"):
            w = WindowAgg(name=key, input_tokens=1200, output_tokens=300, cache_tokens=8500, cost=4.75)
            w.models["claude-opus-4-8"] = ModelAgg(
                model="claude-opus-4-8", input_tokens=1200, output_tokens=300, cache_tokens=8500, cost=4.75
            )
            windows[key] = w
        recs = [
            UsageRecord(
                ts=now - 3600, model_raw="claude-opus-4-8", model_norm="claude-opus-4-8", known=True,
                input_tokens=1200, output_tokens=300, cache_read=8500, cache_creation=0, cost=4.75,
            )
        ]
        from cc_usage.ratelimits import Bucket

        state = RenderState(
            windows=windows,
            buckets=[
                Bucket(key="five_hour", label="5-HOUR", used_percentage=33.0, resets_at=now + 7860),
                Bucket(key="seven_day", label="WEEKLY", used_percentage=60.0, resets_at=now + 200000),
            ],
            now=now,
            config=Config(default_window="all"),
            interval=5,
            rl_present=True,
            heartbeat=series(recs, now, "24h", "cost"),
        )
        out = _plain(build_panel(state), width=100)
        golden = (Path(__file__).parent / "fixtures" / "panel_single_account.txt").read_text("utf-8")
        assert out == golden
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_aggregate_accounts_idle_account_gets_a_zero_row():
    """`all` scope lists every Claude account, even one with no in-window activity."""
    rows = aggregate_accounts([_rec("personal", cost=2.0, inp=100)], NOW, "all", ["personal", "rdqcc"])
    by = {a.label: a for a in rows}
    assert by["rdqcc"].total_tokens == 0 and by["rdqcc"].cost == 0.0


# ── R5: per-account limits ──────────────────────────────────────────────────────
_CLAUDE_RESPONSE = {
    "limits": [
        {"kind": "session", "percent": 33, "resets_at": "2026-07-13T00:50:00+00:00", "scope": None},
    ]
}


def test_account_buckets_labeled_per_account_when_multi():
    captures = {
        "claude:personal": normalize_claude_limits(_CLAUDE_RESPONSE, now=1),
        "claude:rdqcc": normalize_claude_limits(_CLAUDE_RESPONSE, now=1),
    }
    buckets = account_buckets(captures, ["personal", "rdqcc"], multi=True)
    labels = [b.label for b in buckets]
    assert labels == ["PERSONAL 5-HOUR", "RDQCC 5-HOUR"]


def test_account_buckets_single_account_prefix_is_unchanged():
    captures = {"claude:personal": normalize_claude_limits(_CLAUDE_RESPONSE, now=1)}
    buckets = account_buckets(captures, ["personal"], multi=False)
    assert [b.label for b in buckets] == ["CLAUDE 5-HOUR"]  # byte-identical to pre-T11


def test_fetch_account_limits_isolates_failures(monkeypatch):
    good = normalize_claude_limits(_CLAUDE_RESPONSE, now=2)
    last_good = normalize_claude_limits(_CLAUDE_RESPONSE, now=0)
    roots = [
        Root("personal", Path("/h/.claude"), Path("/h/.claude/projects"), "auto"),
        Root("rdqcc", Path("/h/.claude-rdqcc"), Path("/h/.claude-rdqcc/projects"), "config"),
    ]

    def fake_claude(credentials_path, *, config_dir=None, **_kw):
        if "rdqcc" in str(credentials_path):
            raise LimitFetchError("rdqcc temporarily unavailable")
        return good

    def boom():
        raise LimitFetchError("no codex")

    monkeypatch.setattr(limits_fetch, "fetch_claude_limits", fake_claude)
    monkeypatch.setattr(limits_fetch, "fetch_codex_limits", boom)

    captures, warnings = fetch_account_limits(roots, {"claude:rdqcc": last_good})
    assert captures["claude:personal"] is good  # the healthy account refreshed
    assert captures["claude:rdqcc"] is last_good  # the failed account kept its last-good
    assert any("rdqcc" in w for w in warnings)


def test_legacy_bare_claude_limits_cache_key_is_pruned(tmp_path):
    """A pre-multi-account provider-limits.json holds a bare `claude` key that the
    per-account renderer can't show. It must be dropped on load (so the panel never
    claims usable-but-unrenderable data) and never re-saved as cruft."""
    path = tmp_path / "provider-limits.json"
    legacy = normalize_claude_limits(_CLAUDE_RESPONSE, now=1)
    codex_capture = {
        "captured_at": 2.0,
        "source": "codex",
        "rate_limits": {"codex_primary": {"used_percentage": 10.0, "resets_at": 999.0}},
    }
    path.write_text(
        json.dumps({"providers": {"claude": legacy, "codex": codex_capture}}), "utf-8"
    )

    loaded = load_limits_cache(path)
    assert set(loaded) == {"codex"}  # bare legacy key pruned, codex kept

    save_limits_cache(loaded, path)  # a later save must not resurrect the cruft
    assert set(json.loads(path.read_text("utf-8"))["providers"]) == {"codex"}

    # Per-account keys round-trip untouched.
    per_account = {"claude:personal": legacy}
    save_limits_cache(per_account, path)
    assert load_limits_cache(path) == per_account


def test_rl_present_ignores_legacy_bare_claude_key():
    """rl_present must reflect only renderable (account-keyed / codex) captures, so a
    stray bare `claude` capture shows the honest 'check provider login' hint instead
    of 'provider data has no usable windows'."""
    eng = _engine_with([_rec("personal", cost=1.0, inp=10)], ["personal"])
    eng.limit_captures = {"claude": normalize_claude_limits(_CLAUDE_RESPONSE, now=1)}
    assert eng.snapshot(NOW).rl_present is False
    eng.limit_captures = {"claude:personal": normalize_claude_limits(_CLAUDE_RESPONSE, now=1)}
    assert eng.snapshot(NOW).rl_present is True


def test_t10_expiry_applies_per_account():
    def cap(resets: float, pct: float) -> dict:
        return {
            "captured_at": NOW,
            "source": "claude",
            "rate_limits": {
                "five_hour": {"used_percentage": pct, "resets_at": resets, "label": "5-HOUR"}
            },
        }

    captures = {"claude:personal": cap(NOW - 10, 80.0), "claude:rdqcc": cap(NOW + 3600, 40.0)}
    buckets = account_buckets(captures, ["personal", "rdqcc"], multi=True)
    state = RenderState(windows={}, buckets=buckets, now=NOW, config=Config(), interval=5)
    out = _plain(limits_block(state, THEME))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    personal = next(ln for ln in lines if "PERSONAL" in ln)
    rdqcc = next(ln for ln in lines if "RDQCC" in ln)
    assert "0%" in personal and "reset" in personal  # expired -> zeroed, per account
    assert "40%" in rdqcc and "resets in" in rdqcc  # the other account untouched


# ── R7: engine root reload ──────────────────────────────────────────────────────
def test_engine_reload_roots_rescans_on_toggle(tmp_path, monkeypatch):
    r1 = _mkroot(tmp_path, "personal")
    r2 = _mkroot(tmp_path, "company")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(_claude_line("b", "b", 200))

    both = [
        Root("personal", r1, r1 / "projects", "auto"),
        Root("company", r2, r2 / "projects", "config"),
    ]
    disabled = [
        Root("personal", r1, r1 / "projects", "auto"),
        Root("company", r2, r2 / "projects", "config", enabled=False),
    ]
    current = {"roots": both}
    monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: current["roots"])
    # Keep the single-auto-root fallback (roots=None) + Codex dirs hermetic.
    import cc_usage.parser as parser_module

    monkeypatch.setattr(parser_module, "PROJECTS_DIR", r1 / "projects")
    # No Codex roots -> keep discovery hermetic (T12: codex dirs come from discovery,
    # not module constants).
    monkeypatch.setattr(engine_module, "discover_codex_roots", lambda *a, **k: [])

    eng = Engine(Config(), cache_path=None)
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"personal", "company"}

    current["roots"] = disabled
    assert eng.reload_roots() is True
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"personal"}  # company excluded

    current["roots"] = both
    assert eng.reload_roots() is True
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"personal", "company"}  # restored

    # No change -> reload_roots reports no change (no needless rescan).
    assert eng.reload_roots() is False


def test_reload_roots_syncs_config_scope(monkeypatch):
    """Disabling the account the scope points at resets the scope to `all` AND syncs
    config.account_scope, so a later save_config can't persist a dead scope."""
    both = [
        Root("personal", Path("/x/.claude"), Path("/x/.claude/projects"), "auto"),
        Root("rdqcc", Path("/x/.claude-rdqcc"), Path("/x/.claude-rdqcc/projects"), "config"),
    ]
    toggled = [
        both[0],
        Root("rdqcc", Path("/x/.claude-rdqcc"), Path("/x/.claude-rdqcc/projects"), "config", enabled=False),
    ]
    current = {"roots": both}
    monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: current["roots"])

    eng = Engine(Config(account_scope="rdqcc"), cache_path=None)
    assert eng.account_scope == "rdqcc"  # valid while rdqcc is enabled

    current["roots"] = toggled
    assert eng.reload_roots() is True
    assert eng.account_scope == "all"
    assert eng.config.account_scope == "all"  # kept in step for the next save_config


def test_mid_scan_root_toggle_discards_stale_scan_and_cache(tmp_path, monkeypatch):
    """The toggle-during-scan race: reload_roots() swaps the parser while a scan of
    the old parser is still running. The engine's generation guard must make that
    stale scan discard itself (ScanCancelled) instead of marking the fresh empty
    parser as scanned, save_cache must refuse to persist the empty swap-in, and the
    relaunched scan of the new root set must produce a correct, warm-startable
    cache. Deterministic: the 'mid-flight' toggle happens re-entrantly inside the
    captured parser's scan call."""
    import cc_usage.parser as parser_module

    r1 = _mkroot(tmp_path, "personal")
    r2 = _mkroot(tmp_path, "company")
    (r1 / "projects" / "s.jsonl").write_text(_claude_line("a", "a", 100))
    (r2 / "projects" / "s.jsonl").write_text(_claude_line("b", "b", 200))
    both = [
        Root("personal", r1, r1 / "projects", "auto"),
        Root("company", r2, r2 / "projects", "config"),
    ]
    toggled = [
        Root("personal", r1, r1 / "projects", "auto"),
        Root("company", r2, r2 / "projects", "config", enabled=False),
    ]
    current = {"roots": both}
    monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: current["roots"])
    monkeypatch.setattr(parser_module, "PROJECTS_DIR", r1 / "projects")
    # No Codex roots -> keep discovery hermetic (T12: codex dirs come from discovery,
    # not module constants).
    monkeypatch.setattr(engine_module, "discover_codex_roots", lambda *a, **k: [])
    monkeypatch.setattr(engine_module, "LIMITS_CACHE_JSON", tmp_path / "limits.json")
    cache = tmp_path / "cache.pkl"

    eng = Engine(Config(), cache_path=cache)
    stale_parser = eng.parser
    orig_scan = stale_parser.scan

    def scan_with_midflight_toggle(progress=None, cancelled=None):
        result = orig_scan(progress=progress, cancelled=cancelled)
        # The user toggles a root off while this "worker" scan is still in flight.
        current["roots"] = toggled
        assert eng.reload_roots() is True
        return result

    monkeypatch.setattr(stale_parser, "scan", scan_with_midflight_toggle)

    with pytest.raises(ScanCancelled):
        eng.scan()  # the stale scan detects the generation bump and discards itself
    assert eng.is_scanned is False
    assert len(stale_parser.records) == 2  # the stale parser did read both roots...
    assert eng.parser.records == []  # ...but the live parser is the fresh swap-in

    eng.save_cache()  # the stale worker's save after the swap must be a no-op
    assert not cache.exists(), "an EMPTY cache was persisted after the root swap"

    eng.scan()  # the relaunched scan reads exactly the new root set
    assert {r.account for r in eng.parser.records} == {"personal"}
    eng.save_cache()
    assert cache.exists()

    # A fresh engine (same toggled roots) warm-starts from the correct, non-empty cache.
    eng2 = Engine(Config(), cache_path=cache)
    eng2.scan()
    assert eng2.parser.stats.lines_read == 0  # served warm, no re-read
    assert {r.account for r in eng2.parser.records} == {"personal"}


# ── T12: Codex multi-root discovery ──────────────────────────────────────────────
def test_codex_discovery_default_only(tmp_path):
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    roots = discover_codex_roots(Config(), home=home, environ={})
    assert [(r.label, r.source, r.enabled) for r in roots] == [("codex", "auto", True)]
    assert roots[0].projects == home / ".codex" / "sessions"


def test_codex_discovery_env_is_additive(tmp_path):
    """CODEX_HOME now ADDS a root instead of replacing ~/.codex (v2.3.0 behaviour
    change): both the auto ~/.codex and the env root are discovered."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    win = _mkcodex(tmp_path, ".codex-win")
    roots = discover_codex_roots(Config(), home=home, environ={"CODEX_HOME": str(win)})
    assert [r.label for r in roots] == ["codex", "win"]
    assert [r.source for r in roots] == ["auto", "env"]
    assert roots[1].projects == win / "sessions"


def test_codex_discovery_env_equal_to_default_is_deduped(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    roots = discover_codex_roots(
        Config(), home=home, environ={"CODEX_HOME": str(home / ".codex")}
    )
    assert len(roots) == 1 and roots[0].source == "auto"


def test_codex_discovery_config_roots_and_missing_skipped(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    extra = _mkcodex(tmp_path, "extra-codex")
    cfg = Config(
        codex_roots=[
            {"path": str(extra), "label": "work"},
            {"path": str(tmp_path / "ghost")},  # missing dir -> skipped silently
        ]
    )
    roots = discover_codex_roots(cfg, home=home, environ={})
    assert [r.label for r in roots] == ["codex", "work"]


def test_codex_labels_reserved_against_claude_and_all(tmp_path):
    """Codex labels share one namespace with Claude: a codex root deriving a label a
    Claude account already holds (or the `all` sentinel) is suffixed, so scoping can
    tell every account apart across providers."""
    home = tmp_path / "home"
    _mkroot(home, ".claude")
    (home / ".codex").mkdir(parents=True)
    claude_work = _mkroot(tmp_path / "cl", ".claude-work")  # claude "work"
    codex_work = _mkcodex(tmp_path / "cx", ".codex-work")   # would derive "work"
    codex_all = _mkcodex(tmp_path / "cy", ".codex-all")     # would derive "all"
    cfg = Config(
        claude_roots=[{"path": str(claude_work)}],
        codex_roots=[{"path": str(codex_work)}, {"path": str(codex_all)}],
    )
    claude = discover_claude_roots(cfg, home=home, environ={})
    codex = discover_codex_roots(cfg, claude_roots=claude, home=home, environ={})
    assert [r.label for r in claude] == ["personal", "work"]
    assert [r.label for r in codex] == ["codex", "work-2", "all-2"]


def test_codex_discovery_enabled_false_and_disabled_roots(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    e = _mkcodex(tmp_path, "ce")
    f = _mkcodex(tmp_path, "cf")
    cfg = Config(
        codex_roots=[
            {"path": str(e), "label": "ce", "enabled": False},  # config hard-disable
            {"path": str(f), "label": "cf"},
        ],
        disabled_roots=[str(f)],  # UI toggle-off
    )
    by = {r.label: r for r in discover_codex_roots(cfg, home=home, environ={})}
    assert by["codex"].enabled is True
    assert by["ce"].enabled is False
    assert by["cf"].enabled is False


# ── T12: Codex account tagging + rollout-state keying ────────────────────────────
def test_records_tagged_by_codex_root(tmp_path):
    """Records from two codex roots carry each root's label + the Codex provider."""
    r1 = _mkcodex(tmp_path, ".codex")
    r2 = _mkcodex(tmp_path, ".codex-win")
    (r1 / "sessions" / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    (r2 / "sessions" / "rollout-b.jsonl").write_text(_codex_lines(inp=200), "utf-8")
    parser = Parser(
        {"gpt-test": {"input": 2.0, "output": 8.0}},
        roots=[(r1 / "sessions", "codex"), (r2 / "sessions", "codex-win")],
    )
    parser.scan()
    assert {r.account: r.input_tokens for r in parser.records} == {"codex": 100, "codex-win": 200}
    assert all(r.provider == CODEX_PROVIDER for r in parser.records)


def test_same_named_rollout_in_two_codex_roots_do_not_collide(tmp_path):
    """Two roots holding a same-basename rollout must not share per-file state
    (offset/model/totals are keyed by the full resolved path, not the basename)."""
    r1 = tmp_path / "a" / ".codex" / "sessions"
    r2 = tmp_path / "b" / ".codex" / "sessions"
    r1.mkdir(parents=True)
    r2.mkdir(parents=True)
    (r1 / "rollout-x.jsonl").write_text(_codex_lines(inp=100, ts="2026-07-12T12:00:00Z"), "utf-8")
    (r2 / "rollout-x.jsonl").write_text(_codex_lines(inp=200, ts="2026-07-12T13:00:00Z"), "utf-8")
    parser = Parser(
        {"gpt-test": {"input": 2.0, "output": 8.0}},
        roots=[(r1, "codex"), (r2, "codex-win")],
    )
    parser.scan()
    assert {r.account: r.input_tokens for r in parser.records} == {"codex": 100, "codex-win": 200}
    assert len(parser._codex_totals) == 2  # distinct per-file token state, no collision
    assert len(parser._file_models) == 2


# ── T12: Codex cache round-trip + invalidation ───────────────────────────────────
def test_codex_cache_round_trips_two_roots(tmp_path):
    r1 = _mkcodex(tmp_path, ".codex")
    r2 = _mkcodex(tmp_path, ".codex-win")
    (r1 / "sessions" / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    (r2 / "sessions" / "rollout-b.jsonl").write_text(_codex_lines(inp=200), "utf-8")
    cache = tmp_path / "cache.pkl"
    both = [(r1 / "sessions", "codex"), (r2 / "sessions", "codex-win")]
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}

    writer = Parser(pricing, cache_path=cache, roots=both)
    writer.scan()
    writer.save_cache()

    warm = Parser(pricing, cache_path=cache, roots=both)
    warm.scan()
    assert warm.stats.lines_read == 0  # warm start read nothing new
    assert {r.account for r in warm.records} == {"codex", "codex-win"}
    assert all(r.provider == CODEX_PROVIDER for r in warm.records)  # provider round-trips


def test_codex_root_toggle_invalidates_cache_and_drops_records(tmp_path):
    r1 = _mkcodex(tmp_path, ".codex")
    r2 = _mkcodex(tmp_path, ".codex-win")
    (r1 / "sessions" / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    (r2 / "sessions" / "rollout-b.jsonl").write_text(_codex_lines(inp=200), "utf-8")
    cache = tmp_path / "cache.pkl"
    both = [(r1 / "sessions", "codex"), (r2 / "sessions", "codex-win")]
    one = [(r1 / "sessions", "codex")]
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}

    first = Parser(pricing, cache_path=cache, roots=both)
    first.scan()
    first.save_cache()
    assert {r.account for r in first.records} == {"codex", "codex-win"}

    # "disable codex-win": fewer roots -> roots fingerprint mismatch -> full rebuild.
    disabled = Parser(pricing, cache_path=cache, roots=one)
    disabled.scan()
    assert {r.account for r in disabled.records} == {"codex"}
    assert disabled.stats.lines_read > 0  # rescanned, not served from stale cache


def test_old_cache_version_discarded_once(tmp_path):
    """A v5 (pre-provider) cache is dropped and rebuilt exactly once on the next scan."""
    import pickle

    import cc_usage.parser as parser_module

    r1 = _mkcodex(tmp_path, ".codex")
    (r1 / "sessions" / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    cache = tmp_path / "cache.pkl"
    roots = [(r1 / "sessions", "codex")]
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}

    warm = Parser(pricing, cache_path=cache, roots=roots)
    warm.scan()
    warm.save_cache()
    data = pickle.loads(cache.read_bytes())
    assert data["version"] == parser_module._CACHE_VERSION  # current version written

    # Rewrite the on-disk cache pretending it is the previous format version.
    data["version"] = parser_module._CACHE_VERSION - 1
    cache.write_bytes(pickle.dumps(data))

    reader = Parser(pricing, cache_path=cache, roots=roots)
    reader.scan()
    assert reader.stats.lines_read > 0  # stale-version cache ignored -> full re-read
    assert {r.account for r in reader.records} == {"codex"}


# ── T12: Codex account scope + by-account rollup ─────────────────────────────────
def test_scope_isolates_a_codex_account():
    records = [
        _rec("personal", cost=2.0, inp=100),
        _rec("codex", cost=1.0, inp=50, provider=CODEX_PROVIDER, model="gpt-test"),
        _rec("codex-win", cost=3.0, inp=200, provider=CODEX_PROVIDER, model="gpt-test"),
    ]
    eng = _engine_with(records, ["personal"], codex_labels=["codex", "codex-win"])
    assert eng.multi_codex is True
    # With >1 codex root, codex accounts join the scope cycle.
    assert eng._scope_accounts() == ["personal", "codex", "codex-win"]

    eng.account_scope = "codex-win"  # isolates that codex root; excludes claude + other codex
    s = eng.snapshot(NOW)
    assert s.windows["all"].input_tokens == 200
    assert eng.range_metrics(NOW - 200, NOW + 200).input_tokens == 200


def test_by_account_rollup_has_a_row_per_codex_account():
    records = [
        _rec("personal", cost=2.0, inp=100),
        _rec("codex", cost=1.0, inp=50, provider=CODEX_PROVIDER, model="gpt-test"),
        _rec("codex-win", cost=3.0, inp=200, provider=CODEX_PROVIDER, model="gpt-test"),
    ]
    eng = _engine_with(records, ["personal"], codex_labels=["codex", "codex-win"])
    s = eng.snapshot(NOW)
    assert {a.label for a in s.accounts} == {"personal", "codex", "codex-win"}
    out = _plain(by_account_block(s, THEME))
    assert "Codex" in out       # the default ~/.codex account renders as "Codex"
    assert "codex-win" in out   # a second codex root shows its own distinct label


def test_single_codex_root_not_isolatable_zero_noise():
    """A single ~/.codex stays lumped into `all` (not a separate scope), so a plain
    one-claude + one-codex machine keeps its exact `all -> personal -> all` cycle."""
    eng = _engine_with(
        [_rec("personal", cost=2.0, inp=100), _rec(CODEX_ACCOUNT, cost=1.0, inp=50)],
        ["personal"],
    )
    assert eng.multi_codex is False
    assert eng._scope_accounts() == ["personal"]  # codex NOT a scope option
    assert eng._valid_scope(CODEX_ACCOUNT) == "all"  # a stale codex scope resets to all


# ── T12: Codex engine reload + mid-scan race guard (R6) ──────────────────────────
def test_engine_reload_roots_rescans_on_codex_toggle(tmp_path, monkeypatch):
    """Toggling a codex root re-discovers + rescans through the same engine path as a
    Claude toggle: the disabled root's records drop, re-enabling restores them."""
    c1 = tmp_path / ".codex" / "sessions"
    c2 = tmp_path / ".codex-win" / "sessions"
    c1.mkdir(parents=True)
    c2.mkdir(parents=True)
    (c1 / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    (c2 / "rollout-b.jsonl").write_text(_codex_lines(inp=200), "utf-8")
    both = [
        Root("codex", tmp_path / ".codex", c1, "auto"),
        Root("codex-win", tmp_path / ".codex-win", c2, "config"),
    ]
    toggled = [both[0], Root("codex-win", tmp_path / ".codex-win", c2, "config", enabled=False)]
    # No Claude roots keeps the fixture hermetic and off the single-default None path.
    monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: [])
    state = {"codex": both}
    monkeypatch.setattr(engine_module, "discover_codex_roots", lambda *a, **k: state["codex"])

    eng = Engine(Config(), cache_path=None)
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"codex", "codex-win"}

    state["codex"] = toggled
    assert eng.reload_roots() is True
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"codex"}  # codex-win excluded

    state["codex"] = both
    assert eng.reload_roots() is True
    eng.scan()
    assert {r.account for r in eng.parser.records} == {"codex", "codex-win"}  # restored

    assert eng.reload_roots() is False  # no change -> no needless rescan


def test_mid_scan_codex_root_toggle_discards_stale_scan_and_cache(tmp_path, monkeypatch):
    """R6/T12: the toggle-during-scan race guard covers codex roots — a codex toggle
    mid-scan swaps the parser, the stale scan discards itself (ScanCancelled), the
    empty swap-in is never persisted, and the relaunch produces a warm-startable cache."""
    c1 = tmp_path / ".codex" / "sessions"
    c2 = tmp_path / ".codex-win" / "sessions"
    c1.mkdir(parents=True)
    c2.mkdir(parents=True)
    (c1 / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    (c2 / "rollout-b.jsonl").write_text(_codex_lines(inp=200), "utf-8")
    both = [
        Root("codex", tmp_path / ".codex", c1, "auto"),
        Root("codex-win", tmp_path / ".codex-win", c2, "config"),
    ]
    toggled = [both[0], Root("codex-win", tmp_path / ".codex-win", c2, "config", enabled=False)]
    monkeypatch.setattr(engine_module, "discover_claude_roots", lambda cfg: [])
    state = {"codex": both}
    monkeypatch.setattr(engine_module, "discover_codex_roots", lambda *a, **k: state["codex"])
    monkeypatch.setattr(engine_module, "LIMITS_CACHE_JSON", tmp_path / "limits.json")
    cache = tmp_path / "cache.pkl"

    eng = Engine(Config(), cache_path=cache)
    stale_parser = eng.parser
    orig_scan = stale_parser.scan

    def scan_with_midflight_toggle(progress=None, cancelled=None):
        result = orig_scan(progress=progress, cancelled=cancelled)
        state["codex"] = toggled  # user toggles a codex root while the scan is in flight
        assert eng.reload_roots() is True
        return result

    monkeypatch.setattr(stale_parser, "scan", scan_with_midflight_toggle)

    with pytest.raises(ScanCancelled):
        eng.scan()  # stale scan detects the generation bump and discards itself
    assert eng.is_scanned is False
    assert len(stale_parser.records) == 2  # the stale parser read both roots...
    assert eng.parser.records == []  # ...but the live parser is the fresh swap-in

    eng.save_cache()  # the stale worker's save after the swap must be a no-op
    assert not cache.exists(), "an EMPTY cache was persisted after the codex root swap"

    eng.scan()  # the relaunch reads exactly the new codex root set
    assert {r.account for r in eng.parser.records} == {"codex"}
    eng.save_cache()
    assert cache.exists()


# ── T12 follow-ups: CODEX_HOME divergence, codex rename, codex pricing ────────────
def test_codex_home_missing_dir_does_not_divert_default_scan(tmp_path, monkeypatch):
    """$CODEX_HOME is additive via discovery, so the single-default legacy scan path
    (`CODEX_DIR`) must stay `~/.codex` even when `CODEX_HOME` is set to a MISSING dir —
    otherwise Settings lists `~/.codex` while the panel scans the gone `$CODEX_HOME`,
    and the default root's data disappears."""
    import importlib

    import cc_usage.paths as paths_module

    home = tmp_path / "home"
    default_sessions = home / ".codex" / "sessions"
    default_sessions.mkdir(parents=True)
    (default_sessions / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    gone = tmp_path / "gone-codex-home"  # deliberately never created

    monkeypatch.setenv("CODEX_HOME", str(gone))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    try:
        importlib.reload(paths_module)
        # CODEX_DIR ignores CODEX_HOME -> stays ~/.codex, not the missing env dir.
        assert paths_module.CODEX_DIR == home / ".codex"
        assert paths_module.CODEX_SESSIONS_DIR == default_sessions

        # Discovery reports exactly that auto root (the missing env dir is skipped)...
        roots = discover_codex_roots(Config(), home=home, environ={"CODEX_HOME": str(gone)})
        assert [(r.label, r.source) for r in roots] == [("codex", "auto")]
        assert roots[0].projects == paths_module.CODEX_SESSIONS_DIR  # listing == scanned dir

        # ...and scanning that dir still finds the data (records not lost to /gone).
        parser = Parser(
            {"gpt-test": {"input": 2.0, "output": 8.0}},
            roots=[(paths_module.CODEX_SESSIONS_DIR, "codex")],
        )
        parser.scan()
        assert {r.account for r in parser.records} == {"codex"}
        assert sum(r.input_tokens for r in parser.records) == 100
    finally:
        monkeypatch.undo()
        importlib.reload(paths_module)  # restore real module-level paths for later tests


def test_codex_label_rename_only_invalidates_cache(tmp_path):
    """Same codex root path, changed label: cached records carry the OLD label, so the
    roots fingerprint must treat the rename as a changed root set and rebuild (codex
    labels are part of _roots_fingerprint, mirroring the Claude-side pin)."""
    r1 = _mkcodex(tmp_path, ".codex")
    (r1 / "sessions" / "rollout-a.jsonl").write_text(_codex_lines(inp=100), "utf-8")
    cache = tmp_path / "cache.pkl"
    pricing = {"gpt-test": {"input": 2.0, "output": 8.0}}

    writer = Parser(pricing, cache_path=cache, roots=[(r1 / "sessions", "codex")])
    writer.scan()
    writer.save_cache()

    renamed = Parser(pricing, cache_path=cache, roots=[(r1 / "sessions", "codex-win")])
    renamed.scan()
    assert renamed.stats.lines_read == 1  # full re-read, not served warm
    assert {r.account for r in renamed.records} == {"codex-win"}  # new label applied


def test_codex_non_default_root_record_is_priced_by_gpt_model_table(tmp_path):
    """Pricing is model-keyed, not provider-keyed: a Codex record from a NON-default
    (codex-win-style) root is priced by the bundled GPT rates just like any other, so a
    future provider-aware pricing refactor can't silently zero it out."""
    from cc_usage.cost import compute_cost, get_rates
    from cc_usage.pricing import load_pricing

    pricing, _ = load_pricing()
    model = "gpt-5.5"  # a bundled GPT model id
    assert get_rates(model, pricing) is not None

    root = _mkcodex(tmp_path, ".codex-win")
    (root / "sessions" / "rollout-a.jsonl").write_text(
        _codex_lines(inp=1000, out=500, model=model), "utf-8"
    )
    parser = Parser(pricing, roots=[(root / "sessions", "codex-win")])
    parser.scan()

    assert len(parser.records) == 1
    rec = parser.records[0]
    assert rec.account == "codex-win" and rec.provider == CODEX_PROVIDER
    assert rec.known is True
    assert rec.cost > 0.0  # priced via the model table, not zeroed by provider
    expected = compute_cost(
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        cache_read=rec.cache_read,
        cache_creation_total=0,
        ephemeral_5m=0,
        ephemeral_1h=0,
        rates=get_rates(model, pricing),
    )
    assert rec.cost == expected
