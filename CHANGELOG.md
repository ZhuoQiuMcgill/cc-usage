# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [VERSIONING.md](VERSIONING.md) for the release policy.

## [Unreleased]

### Added

- **Persistent parse cache for near-instant warm startup.** The incremental parse state
  (per-file offsets + extracted records + the dedup set) is now saved to
  `~/.config/cc-usage/parse-cache.pkl` and reloaded on the next launch, so a relaunch
  re-parses only transcript bytes appended since last time instead of the entire corpus.
  On a real ~3 GB / 6k-file history this took cold startup from ~7 s to under 1 s (~10×),
  with warm-start totals byte-identical to a cold scan. The cache is pure derived data:
  it's invalidated automatically on a pricing-table change, a deleted transcript, or a
  format-version bump, and a missing/corrupt/incompatible cache degrades silently to a
  full scan (never a crash). Safe to delete at any time.

### Changed

- **Startup no longer blocks the UI.** The first (potentially multi-second) transcript
  scan now runs in a background worker thread: the panel paints immediately with a brief
  `scanning transcripts…` placeholder and fills in when the scan completes, instead of
  freezing until it's done.
- **Optional `orjson` acceleration.** If the [`orjson`](https://github.com/ijl/orjson)
  package is present it's used to parse transcript JSON (~2× faster on this workload);
  it is not a dependency and the stdlib `json` path (with tolerant UTF-8 decode) is used
  otherwise. This only affects the rare cold scan — warm starts read almost nothing.

## [2.1.4] - 2026-07-01

### Fixed

- **`--update` could silently no-op on a pinned `uv tool` install.** The v2.1.3 uv-tool
  fix routed the plain `--update` path through a bare `uv tool upgrade`, which re-resolves
  against whatever source the tool is *currently* recorded as using. If that install was
  ever pinned to a specific rev — by a prior `--update-pr` / `--update-prerelease` /
  `--update-stable` call, or by a user following the README's own "pin a specific release"
  instructions — `uv tool upgrade` silently exits 0 with "Nothing to upgrade" even when a
  newer release exists, so `ccusage --update` would falsely report success while actually
  changing nothing. The uv backend now always force-installs the freshly resolved release
  tag explicitly (`uv tool install --force git+...@<tag>`), the same way the force-reinstall
  commands already did, regardless of any existing pin. The README's own "pin a release,
  then `uv tool upgrade` later" advice had the same bug and is corrected too.

## [2.1.3] - 2026-07-01

### Fixed

- **`--update*` now works on a `uv tool` install.** The built-in self-update commands
  (`--update`, `--update-pr`, `--update-prerelease`, `--update-stable`) always shelled out
  to `pip`, which a `uv tool install` environment doesn't have — so there they failed with
  a generic "could not run pip" instead of upgrading anything. The updater now detects a
  pip-less environment with `uv` on `PATH` and routes through `uv tool upgrade` (plain
  `--update`) or `uv tool install --force git+...@<ref>` (the force-reinstall commands)
  instead, matching the commands the README already told uv users to run by hand.
- **A clear message when Windows blocks replacing ccusage's own running executable.**
  Upgrading ccusage while ccusage itself drives the upgrade means its own launcher `.exe`
  is open; Windows (unlike Unix) refuses to overwrite a running executable's image, so
  pip/uv can install the new package but the final entry-point step fails with a bare
  Win32 "being used by another process" error. The updater now recognizes that specific
  failure and prints a clear hint instead: the package is usually updated already, and
  closing other running `ccusage` windows (or running the update once via
  `python -m cc_usage` instead of `ccusage`) refreshes the command too.

## [2.1.2] - 2026-07-01

### Added

- **Claude Sonnet 5 pricing.** `claude-sonnet-5` is now in the bundled pricing
  table ($3 input / $15 output per 1M tokens), so its usage is costed instead of
  flagged unknown ($0). Cache read/write costs derive from the input rate as
  usual, and the id is matched tolerantly (`[1m]` / date suffixes stripped).
  Sonnet 5's introductory $2/$10 rate (through 2026-08-31) can be set in the
  user-editable `pricing.json` if you prefer the promo rate.

## [2.1.1] - 2026-06-21

### Changed

- **Docs:** the README now recommends installing with **uv** (`uv tool install`),
  keeping pipx and pip-in-a-venv as alternatives, and clarifies that the built-in
  `ccusage --update*` commands need a pip-bearing environment — so `uv tool` installs
  upgrade with `uv tool upgrade cc-usage` instead.

## [2.1.0] - 2026-06-21

### Added

- **Date-range usage analysis.** A keyboard-only analysis screen (open with `d`)
  showing usage metrics for an arbitrary inclusive start → end calendar range:
  totals (tokens in/out/cache, API-equivalent cost, active days, record count), a
  by-model table, a by-day breakdown, and a daily braille chart. Pick the range
  with arrow keys — a preset picker (last 7/30 days, this/last month, all time,
  custom) and a date stepper (↑/↓ ±1 day, ←/→ ±1 month, PgUp/PgDn ±1 year); day
  boundaries use the local calendar.
- **Test-channel self-update commands.** Install unreleased / test builds from the
  CLI and return cleanly to the official release:
  - `ccusage --update-pr <N>` force-reinstalls the head of **open PR #N**
    (`refs/pull/<N>/head`) for testing — the only way to reach unmerged code. It
    cautions that the code is unreviewed and prints how to return to stable.
  - `ccusage --update-prerelease` force-reinstalls the newest prerelease GitHub
    release, falling back to `@main` when none is published.
  - `ccusage --update-stable` force-reinstalls the latest stable release tag (the
    return-to-official path).
  - `ccusage --check-prerelease` reports the installed version against the latest
    prerelease tag and installs nothing.

  Like the existing `--update` / `--check-update`, these touch the network only as
  explicit user actions, need no credentials, and are never reached from the
  panel/data path. Force-reinstall is used because feature/test builds do not bump
  `__version__`. README and VERSIONING.md document the commands and a one-time
  manual bootstrap (a stable build doesn't yet have the `--update-*` flags).

## [2.0.0] - 2026-06-19

First public release: a keyboard-first Claude Code usage TUI, packaged as a
formal, pip-installable tool.

### Added

- **Keyboard-first usage TUI.** Per-model token counts and API-equivalent cost
  across all Claude Code sessions, a usage heartbeat, rolling spend windows
  (1h/5h/24h/7d/all-time), and the official 5h/7d subscription limits captured
  reversibly from the statusline. Operable entirely with arrow keys + Enter.
- **`ccusage` console entry point.** Installs the `ccusage` command (the
  run-from-source launcher matches).
- **Self-update commands.** `ccusage --check-update` reports the installed
  version against the latest GitHub release; `ccusage --update` upgrades to it
  via pip. These are the only commands that touch the network.
- **Single-sourced version.** `cc_usage.__version__` is the one source of truth;
  `pyproject.toml` reads it dynamically. Exposed via `ccusage --version`.
- **Packaging metadata.** MIT license metadata, project URLs, and Python
  classifiers (3.10–3.13).
- **GitHub Actions.** A CI workflow runs the test suite; a Release workflow cuts
  a GitHub Release on each `v*` tag, with PyPI publishing gated on a
  `PYPI_API_TOKEN` secret.

[Unreleased]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.4...HEAD
[2.1.4]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.3...v2.1.4
[2.1.3]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.2...v2.1.3
[2.1.2]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.1...v2.1.2
[2.1.1]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/ZhuoQiuMcgill/cc-usage/releases/tag/v2.0.0
