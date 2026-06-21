"""Command-line entry point (T3 R4) — deliberately tiny.

    ccusage                    launch the full interactive TUI (default; keyboard-only)
    ccusage --once             print a single static frame and exit (for scripts/statusline)
    ccusage --check-update     report current vs latest GitHub release (installs nothing)
    ccusage --update           upgrade to the latest GitHub release via pip
    ccusage --update-pr <N>    install the head of open PR #N for testing (UNREVIEWED code)
    ccusage --update-prerelease install the latest prerelease build (or @main) for testing
    ccusage --update-stable    return to the latest official release
    ccusage --check-prerelease report current vs latest prerelease tag (installs nothing)
    ccusage --version          print the version
    ccusage --help             usage

No flag is *required* for normal use: everything — viewing, switching the heartbeat,
and ALL configuration (incl. the statusline install/restore) — happens inside the TUI
with arrow keys + Enter (the overriding T3 principle). The statusline install/restore
flags survive only as **hidden** scriptable aliases; the in-app Settings path is primary.

The --update / --check-update commands are explicit user actions and may reach the
network; the passive panel/data path remains strictly no-network.
"""

from __future__ import annotations

import argparse

from . import __version__
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccusage",
        description="Interactive panel of Claude Code usage (tokens + API-equivalent "
        "cost) across all sessions, plus the 5h/7d subscription limits. "
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
    # Hidden scriptable aliases for the reversible statusline capture. The primary,
    # documented path is in-app: ccusage -> Settings -> Statusline 5h/7d capture.
    p.add_argument("--install-statusline", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--restore-statusline", action="store_true", help=argparse.SUPPRESS)
    return p


def run_once(config) -> None:
    """Render a single frame and exit (T3 R4 --once)."""
    from rich.console import Console

    from .engine import Engine
    from .render import build_panel

    engine = Engine(config)
    engine.scan()
    Console().print(build_panel(engine.snapshot()))


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

    if args.install_statusline or args.restore_statusline:
        from .statusline import format_result, install, restore

        result = install() if args.install_statusline else restore()
        print(format_result(result))
        return 0 if result.get("ok") else 1

    config = load_config()

    if args.once:
        run_once(config)
        return 0

    from .app import run_tui

    run_tui(config)
    return 0
