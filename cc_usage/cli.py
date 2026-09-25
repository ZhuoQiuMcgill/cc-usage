"""Command-line entry point (T3 R4) — deliberately tiny.

    ccusage                    launch the full interactive TUI (default; keyboard-only)
    ccusage --once             print a single static frame and exit (for scripts)
    ccusage --check-update     report current vs latest GitHub release (installs nothing)
    ccusage --update           upgrade to the latest GitHub release via pip
    ccusage --update-pr <N>    install the head of open PR #N for testing (UNREVIEWED code)
    ccusage --update-prerelease install the latest prerelease build (or @main) for testing
    ccusage --update-stable    return to the latest official release
    ccusage --check-prerelease report current vs latest prerelease tag (installs nothing)
    ccusage --ledger-info      show what the durable usage ledger holds (read-only)
    ccusage --version          print the version
    ccusage --help             usage

No flag is required for normal use. Provider usage limits refresh in the background.
A hidden restore command exists only to remove integrations installed by older versions.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from . import __version__
from .config import load_config


def _configure_unicode_output(stream, *, windows: bool | None = None) -> None:
    """Prevent redirected Windows output from failing on the Unicode dashboard."""
    if windows is None:
        windows = os.name == "nt"
    if not windows or str(getattr(stream, "encoding", "")).lower().replace("-", "") == "utf8":
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccusage",
        description="Interactive panel of Claude Code and Codex usage (tokens + API-equivalent "
        "cost) across all sessions, plus live provider-reported subscription limits. "
        "Launch it and drive everything with arrow keys + Enter — no flags to memorize.",
        # Exact option names only: --update must never match --update-pr etc.
        allow_abbrev=False,
    )
    p.add_argument("--once", action="store_true", help="print a single static frame and exit")
    p.add_argument("--version", action="version", version=f"ccusage {__version__}")
    p.add_argument(
        "--check-update",
        action="store_true",
        help="check whether a newer ccusage release is available (installs nothing)",
    )
    p.add_argument(
        "--update",
        action="store_true",
        help="upgrade ccusage to the latest GitHub release",
    )
    # Test-channel commands (explicit, network-allowed; never the panel/data path).
    p.add_argument(
        "--update-pr",
        metavar="N",
        type=int,
        default=None,
        help="install the head of open PR #N for testing (force-reinstall; UNREVIEWED code)",
    )
    p.add_argument(
        "--update-prerelease",
        action="store_true",
        help="install the latest prerelease build (or @main) for testing (force-reinstall)",
    )
    p.add_argument(
        "--update-stable",
        action="store_true",
        help="return to the latest official release (force-reinstall)",
    )
    p.add_argument(
        "--check-prerelease",
        action="store_true",
        help="report current vs latest prerelease tag (installs nothing)",
    )
    p.add_argument(
        "--ledger-info",
        action="store_true",
        help="show what the usage ledger holds and whether any parsed usage is missing "
        "from it (read-only; check this before shortening Claude Code's transcript retention)",
    )
    # Legacy cleanup only: new versions never install or depend on a statusline.
    p.add_argument("--restore-statusline", action="store_true", help=argparse.SUPPRESS)
    return p


def run_once(config) -> None:
    """Render a single frame and exit (T3 R4 --once)."""
    from rich.console import Console

    from .engine import Engine
    from .render import build_panel

    _configure_unicode_output(sys.stdout)
    engine = Engine(config)
    try:
        engine.scan()
        # Record the parse in the usage ledger (T17) before the cache, so anything the
        # ledger could not take is saved as still pending and retried next run.
        engine.sync_ledger()
        engine.refresh_limits()
        # Persist parse state right after the (expensive) scan — before rendering — so
        # the next launch starts warm even if the terminal render hiccups.
        engine.save_cache()
        Console().print(build_panel(engine.snapshot()))
    finally:
        engine.close()


def run_ledger_info(config, out=None) -> int:
    """Print what the usage ledger holds (T17 R9) and how it compares with the live
    transcripts. Read-only and local: it opens the ledger with SQLite's read-only mode,
    reads the parse cache without saving it, and never writes the ledger, the cache or
    any transcript, nor touches the network."""
    import datetime

    from .engine import Engine
    from .format import human_bytes
    from .ledger import LedgerError, read_summary

    out = out or sys.stdout
    _configure_unicode_output(out)

    def say(line: str = "") -> None:
        print(line, file=out)

    engine = Engine(config)
    path = engine.ledger_path
    say("ccusage usage ledger")
    say(f"  file        {path}")
    if path is None or not path.exists():
        say("  status      no ledger yet: launch ccusage (or run `ccusage --once`) to create it")
        return 0
    try:
        summary = read_summary(path)
    except LedgerError as exc:
        say(f"  status      unreadable: {exc}")
        say("  The next ccusage launch moves an unreadable ledger aside and starts a new one.")
        return 1

    size = summary.size_bytes
    say(f"  size        {human_bytes(size)} ({size:,} bytes, with any WAL)")
    providers = " · ".join(
        f"{name} {count:,}" for name, count in sorted(summary.rows_by_provider.items())
    )
    say(f"  records     {summary.rows:,}" + (f"  ({providers})" if providers else ""))

    identities = engine.ledger_identities()
    current = {(prov, ident): label for label, (prov, ident, _l) in identities.items()}
    enabled = {r.label for r in (*engine.roots, *engine.codex_roots) if r.enabled}
    disabled = {key for key, label in current.items() if label not in enabled}
    parts = []
    for label, provider, identity, count in summary.accounts:
        shown = current.get((provider, identity))
        if shown is None:
            parts.append(f"{label} {count:,} (root no longer configured)")
        elif shown in enabled:
            parts.append(f"{shown} {count:,}")
        else:
            parts.append(f"{shown} {count:,} (disabled)")
    if parts:
        say(f"  accounts    {' · '.join(parts)}")
    if summary.first_ts is not None and summary.last_ts is not None:
        first = datetime.datetime.fromtimestamp(summary.first_ts).date().isoformat()
        last = datetime.datetime.fromtimestamp(summary.last_ts).date().isoformat()
        say(f"  covers      {first} → {last} (local dates)")

    out.flush()
    print("  (comparing with your transcripts…)", file=sys.stderr)
    engine.scan()
    live = engine.parser.live_index()
    orphans = unrecorded = skipped = 0
    for key, account_id in summary.keys.items():
        provider, identity, _label = summary.account_ids.get(account_id, ("", "", ""))
        if (provider, identity) in disabled:
            skipped += 1
        elif key not in live:
            orphans += 1
    for key in live:
        if key not in summary.keys:
            unrecorded += 1
    say(f"  orphans     {orphans:,}  (usage whose transcripts are gone; kept only by the ledger)")
    say(f"  unrecorded  {unrecorded:,}  (parsed usage not in the ledger yet)")
    if skipped:
        say(f"  disabled    {skipped:,}  (rows from disabled roots, not compared)")
    say()
    if unrecorded:
        advice = (
            f"{unrecorded:,} parsed usage records are not in the ledger yet. Launch ccusage "
            "(or run `ccusage --once`) to record them before you shorten Claude Code's "
            "transcript retention."
        )
    else:
        advice = (
            "Every parsed usage record is in the ledger: usage from transcripts that "
            "Claude Code deletes later stays in ccusage."
        )
    for line in textwrap.wrap(advice, width=88):
        say(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Explicit self-update commands (network allowed; never the panel/data path).

    if args.check_update:
        from .update import check_update

        return check_update()

    if args.update:
        from .update import perform_update

        return perform_update()

    # Test-channel self-update commands (network allowed; never the panel/data path).
    if args.update_pr is not None:
        from .update import perform_update_pr

        return perform_update_pr(args.update_pr)

    if args.update_prerelease:
        from .update import perform_update_prerelease

        return perform_update_prerelease()

    if args.update_stable:
        from .update import perform_update_stable

        return perform_update_stable()

    if args.check_prerelease:
        from .update import check_prerelease

        return check_prerelease()

    if args.restore_statusline:
        from .statusline import format_result, restore

        result = restore()
        print(format_result(result))
        return 0 if result.get("ok") else 1

    config = load_config()

    if args.ledger_info:
        return run_ledger_info(config)

    if args.once:
        run_once(config)
        return 0

    from .app import run_tui

    run_tui(config)
    return 0
