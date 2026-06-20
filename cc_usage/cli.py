"""Command-line entry point (T3 R4) — deliberately tiny.

    cc-usage                 launch the full interactive TUI (default; keyboard-only)
    cc-usage --once          print a single static frame and exit (for scripts/statusline)
    cc-usage --version       print the version
    cc-usage --help          usage

No flag is *required* for normal use: everything — viewing, switching the heartbeat,
and ALL configuration (incl. the statusline install/restore) — happens inside the TUI
with arrow keys + Enter (the overriding T3 principle). The statusline install/restore
flags survive only as **hidden** scriptable aliases; the in-app Settings path is primary.
"""

from __future__ import annotations

import argparse

from . import __version__
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cc-usage",
        description="Interactive panel of Claude Code usage (tokens + API-equivalent "
        "cost) across all sessions, plus the 5h/7d subscription limits. "
        "Launch it and drive everything with arrow keys + Enter — no flags to memorize.",
    )
    p.add_argument("--once", action="store_true", help="print a single static frame and exit")
    p.add_argument("--version", action="version", version=f"cc-usage {__version__}")
    # Hidden scriptable aliases for the reversible statusline capture. The primary,
    # documented path is in-app: cc-usage -> Settings -> Statusline 5h/7d capture.
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
