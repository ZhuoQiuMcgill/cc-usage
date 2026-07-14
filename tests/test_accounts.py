"""Multi-account support (T11): root discovery, account tagging, scope filtering,
the by-account rollup (incl. the single-account zero-noise regression pin), per-account
limits, and root enable/disable. Hermetic — synthetic tmp roots and stubbed fetch seams;
no real ~/.claude read and no network."""

from __future__ import annotations

import io
import json
from pathlib import Path

from rich.console import Console

import cc_usage.engine as engine_module
import cc_usage.limits_fetch as limits_fetch
from cc_usage.accounts import (
    CODEX_ACCOUNT,
    Root,
    discover_claude_roots,
)
from cc_usage.aggregate import aggregate_accounts
from cc_usage.config import Config
from cc_usage.engine import Engine
from cc_usage.limits_fetch import (
    LimitFetchError,
    fetch_account_limits,
    normalize_claude_limits,
)
from cc_usage.parser import Parser, UsageRecord
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


# ── R3/R4 engine helpers ────────────────────────────────────────────────────────
def _rec(account: str, age: float = 100.0, *, cost: float, inp: int, model="claude-opus-4-8"):
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
    )


def _engine_with(records, labels):
    eng = Engine(Config(), cache_path=None)
    eng.roots = [
        Root(label, Path(f"/x/{label}"), Path(f"/x/{label}/projects"), "auto" if i == 0 else "config")
        for i, label in enumerate(labels)
    ]
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
    import os
    import time

    os.environ["TZ"] = "UTC"
    time.tzset()
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
    monkeypatch.setattr(engine_module, "CODEX_SESSIONS_DIR", tmp_path / "no-codex")
    monkeypatch.setattr(engine_module, "CODEX_ARCHIVED_SESSIONS_DIR", tmp_path / "no-codex2")

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
